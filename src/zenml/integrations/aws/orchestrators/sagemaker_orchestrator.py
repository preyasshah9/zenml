#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Implementation of the SageMaker orchestrator."""

import os
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)
from uuid import UUID

import boto3
from botocore.exceptions import WaiterError
from sagemaker.estimator import Estimator
from sagemaker.inputs import TrainingInput
from sagemaker.network import NetworkConfig
from sagemaker.processing import ProcessingInput, ProcessingOutput, Processor
from sagemaker.session import Session
from sagemaker.workflow.entities import PipelineVariable
from sagemaker.workflow.execution_variables import (
    ExecutionVariables,
)
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.step_collections import (
    StepCollection,
)
from sagemaker.workflow.steps import (
    ProcessingStep,
    Step,
    TrainingStep,
)
from sagemaker.workflow.triggers import PipelineSchedule

from zenml.client import Client
from zenml.config.base_settings import BaseSettings
from zenml.constants import (
    METADATA_ORCHESTRATOR_LOGS_URL,
    METADATA_ORCHESTRATOR_RUN_ID,
    METADATA_ORCHESTRATOR_URL,
)
from zenml.enums import (
    ExecutionStatus,
    MetadataResourceTypes,
    StackComponentType,
)
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import (
    SagemakerOrchestratorConfig,
    SagemakerOrchestratorSettings,
)
from zenml.integrations.aws.orchestrators.sagemaker_orchestrator_entrypoint_config import (
    SAGEMAKER_PROCESSOR_STEP_ENV_VAR_SIZE_LIMIT,
    SagemakerEntrypointConfiguration,
)
from zenml.logger import get_logger
from zenml.metadata.metadata_types import MetadataType, Uri
from zenml.orchestrators import ContainerizedOrchestrator
from zenml.orchestrators.utils import get_orchestrator_run_name
from zenml.stack import StackValidator
from zenml.utils.env_utils import split_environment_variables
from zenml.utils.time_utils import to_utc_timezone, utc_now_tz_aware

if TYPE_CHECKING:
    from zenml.models import PipelineDeploymentResponse, PipelineRunResponse
    from zenml.stack import Stack

ENV_ZENML_SAGEMAKER_RUN_ID = "ZENML_SAGEMAKER_RUN_ID"
MAX_POLLING_ATTEMPTS = 100
POLLING_DELAY = 30

logger = get_logger(__name__)


def dissect_schedule_arn(
    schedule_arn: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Extracts the region and the name from an EventBridge schedule ARN.

    Args:
        schedule_arn: The ARN of the EventBridge schedule.

    Returns:
        Region Name, Schedule Name (including the group name)

    Raises:
        ValueError: If the input is not a properly formatted ARN.
    """
    # Split the ARN into parts
    arn_parts = schedule_arn.split(":")

    # Validate ARN structure
    if len(arn_parts) < 6 or not arn_parts[5].startswith("schedule/"):
        raise ValueError("Invalid EventBridge schedule ARN format.")

    # Extract the region
    region = arn_parts[3]

    # Extract the group name and schedule name
    name = arn_parts[5].split("schedule/")[1]

    return region, name


def dissect_pipeline_execution_arn(
    pipeline_execution_arn: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract region name, pipeline name, and execution id from the ARN.

    Args:
        pipeline_execution_arn: the pipeline execution ARN

    Returns:
        Region Name, Pipeline Name, Execution ID in order
    """
    # Extract region_name
    region_match = re.search(r"sagemaker:(.*?):", pipeline_execution_arn)
    region_name = region_match.group(1) if region_match else None

    # Extract pipeline_name
    pipeline_match = re.search(
        r"pipeline/(.*?)/execution", pipeline_execution_arn
    )
    pipeline_name = pipeline_match.group(1) if pipeline_match else None

    # Extract execution_id
    execution_match = re.search(r"execution/(.*)", pipeline_execution_arn)
    execution_id = execution_match.group(1) if execution_match else None

    return region_name, pipeline_name, execution_id


class SagemakerOrchestrator(ContainerizedOrchestrator):
    """Orchestrator responsible for running pipelines on Sagemaker."""

    @property
    def config(self) -> SagemakerOrchestratorConfig:
        """Returns the `SagemakerOrchestratorConfig` config.

        Returns:
            The configuration.
        """
        return cast(SagemakerOrchestratorConfig, self._config)

    @property
    def validator(self) -> Optional[StackValidator]:
        """Validates the stack.

        In the remote case, checks that the stack contains a container registry,
        image builder and only remote components.

        Returns:
            A `StackValidator` instance.
        """

        def _validate_remote_components(
            stack: "Stack",
        ) -> Tuple[bool, str]:
            for component in stack.components.values():
                if not component.config.is_local:
                    continue

                return False, (
                    f"The Sagemaker orchestrator runs pipelines remotely, "
                    f"but the '{component.name}' {component.type.value} is "
                    "a local stack component and will not be available in "
                    "the Sagemaker step.\nPlease ensure that you always "
                    "use non-local stack components with the Sagemaker "
                    "orchestrator."
                )

            return True, ""

        return StackValidator(
            required_components={
                StackComponentType.CONTAINER_REGISTRY,
                StackComponentType.IMAGE_BUILDER,
            },
            custom_validation_function=_validate_remote_components,
        )

    def get_orchestrator_run_id(self) -> str:
        """Returns the run id of the active orchestrator run.

        Important: This needs to be a unique ID and return the same value for
        all steps of a pipeline run.

        Returns:
            The orchestrator run id.

        Raises:
            RuntimeError: If the run id cannot be read from the environment.
        """
        try:
            return os.environ[ENV_ZENML_SAGEMAKER_RUN_ID]
        except KeyError:
            raise RuntimeError(
                "Unable to read run id from environment variable "
                f"{ENV_ZENML_SAGEMAKER_RUN_ID}."
            )

    @property
    def settings_class(self) -> Optional[Type["BaseSettings"]]:
        """Settings class for the Sagemaker orchestrator.

        Returns:
            The settings class.
        """
        return SagemakerOrchestratorSettings

    def _get_sagemaker_session(self) -> Session:
        """Method to create the sagemaker session with proper authentication.

        Returns:
            The Sagemaker Session.

        Raises:
            RuntimeError: If the connector returns the wrong type for the
                session.
        """
        # Get authenticated session
        # Option 1: Service connector
        boto_session: boto3.Session
        if connector := self.get_connector():
            boto_session = connector.connect()
            if not isinstance(boto_session, boto3.Session):
                raise RuntimeError(
                    f"Expected to receive a `boto3.Session` object from the "
                    f"linked connector, but got type `{type(boto_session)}`."
                )
        # Option 2: Explicit configuration
        # Args that are not provided will be taken from the default AWS config.
        else:
            boto_session = boto3.Session(
                aws_access_key_id=self.config.aws_access_key_id,
                aws_secret_access_key=self.config.aws_secret_access_key,
                region_name=self.config.region,
                profile_name=self.config.aws_profile,
            )
            # If a role ARN is provided for authentication, assume the role
            if self.config.aws_auth_role_arn:
                sts = boto_session.client("sts")
                response = sts.assume_role(
                    RoleArn=self.config.aws_auth_role_arn,
                    RoleSessionName="zenml-sagemaker-orchestrator",
                )
                credentials = response["Credentials"]
                boto_session = boto3.Session(
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"],
                    region_name=self.config.region,
                )
        return Session(
            boto_session=boto_session, default_bucket=self.config.bucket
        )

    def prepare_or_run_pipeline(
        self,
        deployment: "PipelineDeploymentResponse",
        stack: "Stack",
        environment: Dict[str, str],
        placeholder_run: Optional["PipelineRunResponse"] = None,
    ) -> Iterator[Dict[str, MetadataType]]:
        """Prepares or runs a pipeline on Sagemaker.

        Args:
            deployment: The deployment to prepare or run.
            stack: The stack to run on.
            environment: Environment variables to set in the orchestration
                environment.
            placeholder_run: An optional placeholder run for the deployment.

        Raises:
            RuntimeError: If there is an error creating or scheduling the
                pipeline.
            TypeError: If the network_config passed is not compatible with the
                AWS SageMaker NetworkConfig class.
            ValueError: If the schedule is not valid.

        Yields:
            A dictionary of metadata related to the pipeline run.
        """
        # sagemaker requires pipelineName to use alphanum and hyphens only
        unsanitized_orchestrator_run_name = get_orchestrator_run_name(
            pipeline_name=deployment.pipeline_configuration.name
        )
        # replace all non-alphanum and non-hyphens with hyphens
        orchestrator_run_name = re.sub(
            r"[^a-zA-Z0-9\-]", "-", unsanitized_orchestrator_run_name
        )

        session = self._get_sagemaker_session()

        # Sagemaker does not allow environment variables longer than 256
        # characters to be passed to Processor steps. If an environment variable
        # is longer than 256 characters, we split it into multiple environment
        # variables (chunks) and re-construct it on the other side using the
        # custom entrypoint configuration.
        split_environment_variables(
            size_limit=SAGEMAKER_PROCESSOR_STEP_ENV_VAR_SIZE_LIMIT,
            env=environment,
        )

        sagemaker_steps = []
        for step_name, step in deployment.step_configurations.items():
            image = self.get_image(deployment=deployment, step_name=step_name)
            command = SagemakerEntrypointConfiguration.get_entrypoint_command()
            arguments = (
                SagemakerEntrypointConfiguration.get_entrypoint_arguments(
                    step_name=step_name, deployment_id=deployment.id
                )
            )
            entrypoint = command + arguments

            step_settings = cast(
                SagemakerOrchestratorSettings, self.get_settings(step)
            )

            if step_settings.environment:
                split_environment = step_settings.environment.copy()
                # Sagemaker does not allow environment variables longer than 256
                # characters to be passed to Processor steps. If an environment variable
                # is longer than 256 characters, we split it into multiple environment
                # variables (chunks) and re-construct it on the other side using the
                # custom entrypoint configuration.
                split_environment_variables(
                    size_limit=SAGEMAKER_PROCESSOR_STEP_ENV_VAR_SIZE_LIMIT,
                    env=split_environment,
                )
                environment.update(split_environment)

            use_training_step = (
                step_settings.use_training_step
                if step_settings.use_training_step is not None
                else (
                    self.config.use_training_step
                    if self.config.use_training_step is not None
                    else True
                )
            )

            # Retrieve Executor arguments provided in the Step settings.
            if use_training_step:
                args_for_step_executor = step_settings.estimator_args or {}
                args_for_step_executor.setdefault(
                    "volume_size", step_settings.volume_size_in_gb
                )
                args_for_step_executor.setdefault(
                    "max_run", step_settings.max_runtime_in_seconds
                )
            else:
                args_for_step_executor = step_settings.processor_args or {}
                args_for_step_executor.setdefault(
                    "volume_size_in_gb", step_settings.volume_size_in_gb
                )
                args_for_step_executor.setdefault(
                    "max_runtime_in_seconds",
                    step_settings.max_runtime_in_seconds,
                )

            # Set default values from configured orchestrator Component to
            # arguments to be used when they are not present in processor_args.
            args_for_step_executor.setdefault(
                "role",
                step_settings.execution_role or self.config.execution_role,
            )

            tags = step_settings.tags
            args_for_step_executor.setdefault(
                "tags",
                (
                    [
                        {"Key": key, "Value": value}
                        for key, value in tags.items()
                    ]
                    if tags
                    else None
                ),
            )

            args_for_step_executor.setdefault(
                "instance_type", step_settings.instance_type
            )

            # Set values that cannot be overwritten
            args_for_step_executor["image_uri"] = image
            args_for_step_executor["instance_count"] = 1
            args_for_step_executor["sagemaker_session"] = session
            args_for_step_executor["base_job_name"] = orchestrator_run_name

            # Convert network_config to sagemaker.network.NetworkConfig if
            # present
            network_config = args_for_step_executor.get("network_config")

            if network_config and isinstance(network_config, dict):
                try:
                    args_for_step_executor["network_config"] = NetworkConfig(
                        **network_config
                    )
                except TypeError:
                    # If the network_config passed is not compatible with the
                    # NetworkConfig class, raise a more informative error.
                    raise TypeError(
                        "Expected a sagemaker.network.NetworkConfig "
                        "compatible object for the network_config argument, "
                        "but the network_config processor argument is invalid."
                        "See https://sagemaker.readthedocs.io/en/stable/api/utility/network.html#sagemaker.network.NetworkConfig "
                        "for more information about the NetworkConfig class."
                    )

            # Construct S3 inputs to container for step
            training_inputs: Optional[
                Union[TrainingInput, Dict[str, TrainingInput]]
            ] = None
            processing_inputs: Optional[List[ProcessingInput]] = None

            if step_settings.input_data_s3_uri is None:
                pass
            elif isinstance(step_settings.input_data_s3_uri, str):
                if use_training_step:
                    training_inputs = TrainingInput(
                        s3_data=step_settings.input_data_s3_uri,
                        input_mode=step_settings.input_data_s3_mode,
                    )
                else:
                    processing_inputs = [
                        ProcessingInput(
                            source=step_settings.input_data_s3_uri,
                            destination="/opt/ml/processing/input/data",
                            s3_input_mode=step_settings.input_data_s3_mode,
                        )
                    ]
            elif isinstance(step_settings.input_data_s3_uri, dict):
                if use_training_step:
                    training_inputs = {}
                    for (
                        channel,
                        s3_uri,
                    ) in step_settings.input_data_s3_uri.items():
                        training_inputs[channel] = TrainingInput(
                            s3_data=s3_uri,
                            input_mode=step_settings.input_data_s3_mode,
                        )
                else:
                    processing_inputs = []
                    for (
                        channel,
                        s3_uri,
                    ) in step_settings.input_data_s3_uri.items():
                        processing_inputs.append(
                            ProcessingInput(
                                source=s3_uri,
                                destination=f"/opt/ml/processing/input/data/{channel}",
                                s3_input_mode=step_settings.input_data_s3_mode,
                            )
                        )

            # Construct S3 outputs from container for step
            outputs = None
            output_path = None

            if step_settings.output_data_s3_uri is None:
                pass
            elif isinstance(step_settings.output_data_s3_uri, str):
                if use_training_step:
                    output_path = step_settings.output_data_s3_uri
                else:
                    outputs = [
                        ProcessingOutput(
                            source="/opt/ml/processing/output/data",
                            destination=step_settings.output_data_s3_uri,
                            s3_upload_mode=step_settings.output_data_s3_mode,
                        )
                    ]
            elif isinstance(step_settings.output_data_s3_uri, dict):
                outputs = []
                for (
                    channel,
                    s3_uri,
                ) in step_settings.output_data_s3_uri.items():
                    outputs.append(
                        ProcessingOutput(
                            source=f"/opt/ml/processing/output/data/{channel}",
                            destination=s3_uri,
                            s3_upload_mode=step_settings.output_data_s3_mode,
                        )
                    )

            step_environment: Dict[str, Union[str, PipelineVariable]] = {
                key: str(value)
                if not isinstance(value, PipelineVariable)
                else value
                for key, value in environment.items()
            }

            step_environment[ENV_ZENML_SAGEMAKER_RUN_ID] = (
                ExecutionVariables.PIPELINE_EXECUTION_ARN
            )

            sagemaker_step: Step
            if use_training_step:
                # Create Estimator and TrainingStep
                estimator = Estimator(
                    keep_alive_period_in_seconds=step_settings.keep_alive_period_in_seconds,
                    output_path=output_path,
                    environment=step_environment,
                    container_entry_point=entrypoint,
                    **args_for_step_executor,
                )

                sagemaker_step = TrainingStep(
                    name=step_name,
                    depends_on=cast(
                        Optional[List[Union[str, Step, StepCollection]]],
                        step.spec.upstream_steps,
                    ),
                    inputs=training_inputs,
                    estimator=estimator,
                )
            else:
                # Create Processor and ProcessingStep
                processor = Processor(
                    entrypoint=cast(
                        Optional[List[Union[str, PipelineVariable]]],
                        entrypoint,
                    ),
                    env=step_environment,
                    **args_for_step_executor,
                )

                sagemaker_step = ProcessingStep(
                    name=step_name,
                    processor=processor,
                    depends_on=cast(
                        Optional[List[Union[str, Step, StepCollection]]],
                        step.spec.upstream_steps,
                    ),
                    inputs=processing_inputs,
                    outputs=outputs,
                )

            sagemaker_steps.append(sagemaker_step)

        # Create the pipeline
        pipeline = Pipeline(
            name=orchestrator_run_name,
            steps=sagemaker_steps,
            sagemaker_session=session,
        )

        settings = cast(
            SagemakerOrchestratorSettings, self.get_settings(deployment)
        )

        pipeline.create(
            role_arn=self.config.execution_role,
            tags=[
                {"Key": key, "Value": value}
                for key, value in settings.pipeline_tags.items()
            ]
            if settings.pipeline_tags
            else None,
        )

        # Handle scheduling if specified
        if deployment.schedule:
            if settings.synchronous:
                logger.warning(
                    "The 'synchronous' setting is ignored for scheduled "
                    "pipelines since they run independently of the "
                    "deployment process."
                )

            schedule_name = orchestrator_run_name
            next_execution = None
            start_date = (
                to_utc_timezone(deployment.schedule.start_time)
                if deployment.schedule.start_time
                else None
            )

            # Create PipelineSchedule based on schedule type
            if deployment.schedule.cron_expression:
                cron_exp = self._validate_cron_expression(
                    deployment.schedule.cron_expression
                )
                schedule = PipelineSchedule(
                    name=schedule_name,
                    cron=cron_exp,
                    start_date=start_date,
                    enabled=True,
                )
            elif deployment.schedule.interval_second:
                # This is necessary because SageMaker's PipelineSchedule rate
                # expressions require minutes as the minimum time unit.
                # Even if a user specifies an interval of less than 60 seconds,
                # it will be rounded up to 1 minute.
                minutes = max(
                    1,
                    int(
                        deployment.schedule.interval_second.total_seconds()
                        / 60
                    ),
                )
                schedule = PipelineSchedule(
                    name=schedule_name,
                    rate=(minutes, "minutes"),
                    start_date=start_date,
                    enabled=True,
                )
                next_execution = (
                    deployment.schedule.start_time or utc_now_tz_aware()
                ) + deployment.schedule.interval_second
            else:
                # One-time schedule
                execution_time = (
                    deployment.schedule.run_once_start_time
                    or deployment.schedule.start_time
                )
                if not execution_time:
                    raise ValueError(
                        "A start time must be specified for one-time "
                        "schedule execution"
                    )
                schedule = PipelineSchedule(
                    name=schedule_name,
                    at=to_utc_timezone(execution_time),
                    enabled=True,
                )
                next_execution = execution_time

            # Get the current role ARN if not explicitly configured
            if self.config.scheduler_role is None:
                logger.info(
                    "No scheduler_role configured. Trying to extract it from "
                    "the client side authentication."
                )
                sts = session.boto_session.client("sts")
                try:
                    scheduler_role_arn = sts.get_caller_identity()["Arn"]
                    # If this is a user ARN, try to get the role ARN
                    if ":user/" in scheduler_role_arn:
                        logger.warning(
                            f"Using IAM user credentials "
                            f"({scheduler_role_arn}). For production "
                            "environments, it's recommended to use IAM roles "
                            "instead."
                        )
                    # If this is an assumed role, extract the role ARN
                    elif ":assumed-role/" in scheduler_role_arn:
                        # Convert assumed-role ARN format to role ARN format
                        # From: arn:aws:sts::123456789012:assumed-role/role-name/session-name
                        # To: arn:aws:iam::123456789012:role/role-name
                        scheduler_role_arn = re.sub(
                            r"arn:aws:sts::(\d+):assumed-role/([^/]+)/.*",
                            r"arn:aws:iam::\1:role/\2",
                            scheduler_role_arn,
                        )
                    elif ":role/" not in scheduler_role_arn:
                        raise RuntimeError(
                            f"Unexpected credential type "
                            f"({scheduler_role_arn}). Please use IAM "
                            f"roles for SageMaker pipeline scheduling."
                        )
                    else:
                        raise RuntimeError(
                            "The ARN of the caller identity "
                            f"`{scheduler_role_arn}` does not "
                            "include a user or a proper role."
                        )
                except Exception:
                    raise RuntimeError(
                        "Failed to get current role ARN. This means the "
                        "your client side credentials that you are "
                        "is not configured correctly to schedule sagemaker "
                        "pipelines. For more information, please check:"
                        "https://docs.zenml.io/stacks/stack-components/orchestrators/sagemaker#required-iam-permissions-for-schedules"
                    )
            else:
                scheduler_role_arn = self.config.scheduler_role

            # Attach schedule to pipeline
            triggers = pipeline.put_triggers(
                triggers=[schedule],
                role_arn=scheduler_role_arn,
            )
            logger.info(f"The schedule ARN is: {triggers[0]}")

            try:
                from zenml.models import RunMetadataResource

                schedule_metadata = self.generate_schedule_metadata(
                    schedule_arn=triggers[0]
                )

                Client().create_run_metadata(
                    metadata=schedule_metadata,  # type: ignore[arg-type]
                    resources=[
                        RunMetadataResource(
                            id=deployment.schedule.id,
                            type=MetadataResourceTypes.SCHEDULE,
                        )
                    ],
                )
            except Exception as e:
                logger.debug(
                    "There was an error attaching metadata to the "
                    f"schedule: {e}"
                )

            logger.info(
                f"Successfully scheduled pipeline with name: {schedule_name}\n"
                + (
                    f"First execution will occur at: "
                    f"{next_execution.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    if next_execution
                    else f"Using cron expression: "
                    f"{deployment.schedule.cron_expression}"
                )
                + (
                    f" (and every {minutes} minutes after)"
                    if deployment.schedule.interval_second
                    else ""
                )
            )
            logger.info(
                "\n\nIn order to cancel the schedule, you can use execute "
                "the following command:\n"
            )
            logger.info(
                f"`aws scheduler delete-schedule --name {schedule_name}`"
            )
        else:
            # Execute the pipeline immediately if no schedule is specified
            execution = pipeline.start()
            logger.warning(
                "Steps can take 5-15 minutes to start running "
                "when using the Sagemaker Orchestrator."
            )

            # Yield metadata based on the generated execution object
            yield from self.compute_metadata(
                execution_arn=execution.arn, settings=settings
            )

            # mainly for testing purposes, we wait for the pipeline to finish
            if settings.synchronous:
                logger.info(
                    "Executing synchronously. Waiting for pipeline to "
                    "finish... \n"
                    "At this point you can `Ctrl-C` out without cancelling the "
                    "execution."
                )
                try:
                    execution.wait(
                        delay=POLLING_DELAY, max_attempts=MAX_POLLING_ATTEMPTS
                    )
                    logger.info("Pipeline completed successfully.")
                except WaiterError:
                    raise RuntimeError(
                        "Timed out while waiting for pipeline execution to "
                        "finish. For long-running pipelines we recommend "
                        "configuring your orchestrator for asynchronous "
                        "execution. The following command does this for you: \n"
                        f"`zenml orchestrator update {self.name} "
                        f"--synchronous=False`"
                    )

    def get_pipeline_run_metadata(
        self, run_id: UUID
    ) -> Dict[str, "MetadataType"]:
        """Get general component-specific metadata for a pipeline run.

        Args:
            run_id: The ID of the pipeline run.

        Returns:
            A dictionary of metadata.
        """
        execution_arn = os.environ[ENV_ZENML_SAGEMAKER_RUN_ID]

        run_metadata: Dict[str, "MetadataType"] = {}

        settings = cast(
            SagemakerOrchestratorSettings,
            self.get_settings(Client().get_pipeline_run(run_id)),
        )

        for metadata in self.compute_metadata(
            execution_arn=execution_arn,
            settings=settings,
        ):
            run_metadata.update(metadata)

        return run_metadata

    def fetch_status(self, run: "PipelineRunResponse") -> ExecutionStatus:
        """Refreshes the status of a specific pipeline run.

        Args:
            run: The run that was executed by this orchestrator.

        Returns:
            the actual status of the pipeline job.

        Raises:
            AssertionError: If the run was not executed by to this orchestrator.
            ValueError: If it fetches an unknown state or if we can not fetch
                the orchestrator run ID.
        """
        # Make sure that the stack exists and is accessible
        if run.stack is None:
            raise ValueError(
                "The stack that the run was executed on is not available "
                "anymore."
            )

        # Make sure that the run belongs to this orchestrator
        assert (
            self.id
            == run.stack.components[StackComponentType.ORCHESTRATOR][0].id
        )

        # Initialize the Sagemaker client
        session = self._get_sagemaker_session()
        sagemaker_client = session.sagemaker_client

        # Fetch the status of the _PipelineExecution
        if METADATA_ORCHESTRATOR_RUN_ID in run.run_metadata:
            run_id = run.run_metadata[METADATA_ORCHESTRATOR_RUN_ID]
        elif run.orchestrator_run_id is not None:
            run_id = run.orchestrator_run_id
        else:
            raise ValueError(
                "Can not find the orchestrator run ID, thus can not fetch "
                "the status."
            )
        status = sagemaker_client.describe_pipeline_execution(
            PipelineExecutionArn=run_id
        )["PipelineExecutionStatus"]

        # Map the potential outputs to ZenML ExecutionStatus. Potential values:
        # https://cloud.google.com/vertex-ai/docs/reference/rest/v1beta1/PipelineState
        if status in ["Executing", "Stopping"]:
            return ExecutionStatus.RUNNING
        elif status in ["Stopped", "Failed"]:
            return ExecutionStatus.FAILED
        elif status in ["Succeeded"]:
            return ExecutionStatus.COMPLETED
        else:
            raise ValueError("Unknown status for the pipeline execution.")

    def compute_metadata(
        self,
        execution_arn: str,
        settings: SagemakerOrchestratorSettings,
    ) -> Iterator[Dict[str, MetadataType]]:
        """Generate run metadata based on the generated Sagemaker Execution.

        Args:
            execution_arn: The ARN of the pipeline execution.
            settings: The Sagemaker orchestrator settings.

        Yields:
            A dictionary of metadata related to the pipeline run.
        """
        # Orchestrator Run ID
        metadata: Dict[str, MetadataType] = {
            "pipeline_execution_arn": execution_arn,
            METADATA_ORCHESTRATOR_RUN_ID: execution_arn,
        }

        # URL to the Sagemaker's pipeline view
        if orchestrator_url := self._compute_orchestrator_url(
            execution_arn=execution_arn
        ):
            metadata[METADATA_ORCHESTRATOR_URL] = Uri(orchestrator_url)

        # URL to the corresponding CloudWatch page
        if logs_url := self._compute_orchestrator_logs_url(
            execution_arn=execution_arn, settings=settings
        ):
            metadata[METADATA_ORCHESTRATOR_LOGS_URL] = Uri(logs_url)

        yield metadata

    def _compute_orchestrator_url(
        self,
        execution_arn: Any,
    ) -> Optional[str]:
        """Generate the Orchestrator Dashboard URL upon pipeline execution.

        Args:
            execution_arn: The ARN of the pipeline execution.

        Returns:
             the URL to the dashboard view in SageMaker.
        """
        try:
            (
                region_name,
                pipeline_name,
                execution_id,
            ) = dissect_pipeline_execution_arn(execution_arn)

            # Get the Sagemaker session
            session = self._get_sagemaker_session()

            # List the Studio domains and get the Studio Domain ID
            domains_response = session.sagemaker_client.list_domains()
            studio_domain_id = domains_response["Domains"][0]["DomainId"]

            return (
                f"https://studio-{studio_domain_id}.studio.{region_name}."
                f"sagemaker.aws/pipelines/view/{pipeline_name}/executions"
                f"/{execution_id}/graph"
            )

        except Exception as e:
            logger.warning(
                f"There was an issue while extracting the pipeline url: {e}"
            )
            return None

    @staticmethod
    def _compute_orchestrator_logs_url(
        execution_arn: Any,
        settings: SagemakerOrchestratorSettings,
    ) -> Optional[str]:
        """Generate the CloudWatch URL upon pipeline execution.

        Args:
            execution_arn: The ARN of the pipeline execution.
            settings: The Sagemaker orchestrator settings.

        Returns:
            the URL querying the pipeline logs in CloudWatch on AWS.
        """
        try:
            region_name, _, execution_id = dissect_pipeline_execution_arn(
                execution_arn
            )

            use_training_jobs = True
            if settings.use_training_step is not None:
                use_training_jobs = settings.use_training_step

            job_type = "Training" if use_training_jobs else "Processing"

            return (
                f"https://{region_name}.console.aws.amazon.com/"
                f"cloudwatch/home?region={region_name}#logsV2:log-groups/log-group"
                f"/$252Faws$252Fsagemaker$252F{job_type}Jobs$3FlogStreamNameFilter"
                f"$3Dpipelines-{execution_id}-"
            )
        except Exception as e:
            logger.warning(
                f"There was an issue while extracting the logs url: {e}"
            )
            return None

    @staticmethod
    def generate_schedule_metadata(schedule_arn: str) -> Dict[str, str]:
        """Attaches metadata to the ZenML Schedules.

        Args:
            schedule_arn: The trigger ARNs that is generated on the AWS side.

        Returns:
            a dictionary containing metadata related to the schedule.
        """
        region, name = dissect_schedule_arn(schedule_arn=schedule_arn)

        return {
            "trigger_url": (
                f"https://{region}.console.aws.amazon.com/scheduler/home"
                f"?region={region}#schedules/{name}"
            ),
        }

    @staticmethod
    def _validate_cron_expression(cron_expression: str) -> str:
        """Validates and formats a cron expression for SageMaker schedules.

        Args:
            cron_expression: The cron expression to validate

        Returns:
            The formatted cron expression

        Raises:
            ValueError: If the cron expression is invalid
        """
        # Strip any "cron(" prefix if it exists
        cron_exp = cron_expression.replace("cron(", "").replace(")", "")

        # Split into components
        parts = cron_exp.split()
        if len(parts) not in [6, 7]:  # AWS cron requires 6 or 7 fields
            raise ValueError(
                f"Invalid cron expression: {cron_expression}. AWS cron "
                "expressions must have 6 or 7 fields: minute hour day-of-month "
                "month day-of-week year(optional). Example: '15 10 ? * 6L "
                "2022-2023'"
            )

        return cron_exp
