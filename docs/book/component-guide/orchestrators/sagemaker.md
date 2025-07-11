---
description: Orchestrating your pipelines to run on Amazon Sagemaker.
---

# AWS Sagemaker Orchestrator

[Sagemaker Pipelines](https://aws.amazon.com/sagemaker/pipelines) is a serverless ML workflow tool running on AWS. It is an easy way to quickly run your code in a production-ready, repeatable cloud orchestrator that requires minimal setup without provisioning and paying for standby compute.

{% hint style="warning" %}
This component is only meant to be used within the context of a [remote ZenML deployment scenario](https://docs.zenml.io/getting-started/deploying-zenml/). Usage with a local ZenML deployment may lead to unexpected behavior!
{% endhint %}

## When to use it

You should use the Sagemaker orchestrator if:

* you're already using AWS.
* you're looking for a proven production-grade orchestrator.
* you're looking for a UI in which you can track your pipeline runs.
* you're looking for a managed solution for running your pipelines.
* you're looking for a serverless solution for running your pipelines.

## How it works

The ZenML Sagemaker orchestrator works with [Sagemaker Pipelines](https://aws.amazon.com/sagemaker/pipelines), which can be used to construct machine learning pipelines. Under the hood, for each ZenML pipeline step, it creates a SageMaker `PipelineStep`, which contains a Sagemaker Processing or Training job.

## How to deploy it

{% hint style="info" %}
Would you like to skip ahead and deploy a full ZenML cloud stack already, including a Sagemaker orchestrator? Check out the[in-browser stack deployment wizard](https://docs.zenml.io/how-to/infrastructure-deployment/stack-deployment/deploy-a-cloud-stack), the [stack registration wizard](https://docs.zenml.io/how-to/infrastructure-deployment/stack-deployment/register-a-cloud-stack), or [the ZenML AWS Terraform module](https://docs.zenml.io/how-to/infrastructure-deployment/stack-deployment/deploy-a-cloud-stack-with-terraform) for a shortcut on how to deploy & register this stack component.
{% endhint %}

In order to use a Sagemaker AI orchestrator, you need to first deploy [ZenML to the cloud](https://docs.zenml.io/getting-started/deploying-zenml/). It would be recommended to deploy ZenML in the same region as you plan on using for Sagemaker, but it is not necessary to do so. You must ensure that you are connected to the remote ZenML server before using this stack component.

The only other thing necessary to use the ZenML Sagemaker orchestrator is enabling the relevant permissions for your particular role.

## How to use it

To use the Sagemaker orchestrator, we need:

* The ZenML `aws` and `s3` integrations installed. If you haven't done so, run

```shell
zenml integration install aws s3
```

* [Docker](https://www.docker.com) installed and running.
* A [remote artifact store](https://docs.zenml.io/stacks/artifact-stores/) as part of your stack (configured with an `authentication_secret` attribute).
* A [remote container registry](https://docs.zenml.io/stacks/container-registries/) as part of your stack.
* An IAM role with specific SageMaker permissions following the principle of least privilege (see [Required IAM Permissions](#required-iam-permissions) below) as well as `sagemaker.amazonaws.com` added as a Principal Service. Avoid using the broad `AmazonSageMakerFullAccess` managed policy in production environments.
* The local client (whoever is running the pipeline) will also need specific permissions to launch SageMaker jobs (see [Required IAM Permissions](#required-iam-permissions) below for the minimal required permissions).
* If you want to use schedules, you also need to set up the correct roles, permissions and policies covered [here](sagemaker.md#required-iam-permissions-for-schedules).

There are three ways you can authenticate your orchestrator and link it to the IAM role you have created:

{% tabs %}
{% tab title="Authentication via Service Connector" %}
The recommended way to authenticate your SageMaker orchestrator is by registering an [AWS Service Connector](https://docs.zenml.io/how-to/infrastructure-deployment/auth-management/aws-service-connector) and connecting it to your SageMaker orchestrator. If you plan to use scheduled pipelines, ensure the credentials used by the service connector have the necessary EventBridge and IAM permissions listed in the [Required IAM Permissions](sagemaker.md#required-iam-permissions) section:

```shell
zenml service-connector register <CONNECTOR_NAME> --type aws -i
zenml orchestrator register <ORCHESTRATOR_NAME> \
    --flavor=sagemaker \
    --execution_role=<YOUR_IAM_ROLE_ARN>
zenml orchestrator connect <ORCHESTRATOR_NAME> --connector <CONNECTOR_NAME>
zenml stack register <STACK_NAME> -o <ORCHESTRATOR_NAME> ... --set
```
{% endtab %}

{% tab title="Explicit Authentication" %}
Instead of creating a service connector, you can also configure your AWS authentication credentials directly in the orchestrator. If you plan to use scheduled pipelines, ensure these credentials have the necessary EventBridge and IAM permissions listed in the [Required IAM Permissions](sagemaker.md#required-iam-permissions) section:

```shell
zenml orchestrator register <ORCHESTRATOR_NAME> \
    --flavor=sagemaker \
    --execution_role=<YOUR_IAM_ROLE_ARN> \ 
    --aws_access_key_id=...
    --aws_secret_access_key=...
    --region=...
zenml stack register <STACK_NAME> -o <ORCHESTRATOR_NAME> ... --set
```

See the [`SagemakerOrchestratorConfig` SDK Docs](https://sdkdocs.zenml.io/latest/integration_code_docs/integrations-aws.html#zenml.integrations.aws) for more information on available configuration options.
{% endtab %}

{% tab title="Implicit Authentication" %}
If you neither connect your orchestrator to a service connector nor configure credentials explicitly, ZenML will try to implicitly authenticate to AWS via the `default` profile in your local [AWS configuration file](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html). If you plan to use scheduled pipelines, ensure this profile has the necessary EventBridge and IAM permissions listed in the [Required IAM Permissions](sagemaker.md#required-iam-permissions) section:

```shell
zenml orchestrator register <ORCHESTRATOR_NAME> \
    --flavor=sagemaker \
    --execution_role=<YOUR_IAM_ROLE_ARN>
zenml stack register <STACK_NAME> -o <ORCHESTRATOR_NAME> ... --set
python run.py  # Authenticates with `default` profile in `~/.aws/config`
```
{% endtab %}
{% endtabs %}

## Required IAM Permissions

Instead of using the broad `AmazonSageMakerFullAccess` managed policy, follow the principle of least privilege by creating custom policies with only the required permissions:

### Execution Role Permissions (for SageMaker jobs)

Create a custom policy for the execution role that SageMaker will assume:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sagemaker:CreateProcessingJob",
        "sagemaker:DescribeProcessingJob",
        "sagemaker:StopProcessingJob",
        "sagemaker:CreateTrainingJob",
        "sagemaker:DescribeTrainingJob",
        "sagemaker:StopTrainingJob"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::your-bucket-name",
        "arn:aws:s3:::your-bucket-name/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:GetLogEvents"
      ],
      "Resource": "*"
    }
  ]
}
```

### Client Permissions (for pipeline submission)

Create a custom policy for the client/user submitting pipelines:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sagemaker:CreatePipeline",
        "sagemaker:StartPipelineExecution",
        "sagemaker:StopPipelineExecution",
        "sagemaker:DescribePipeline",
        "sagemaker:DescribePipelineExecution",
        "sagemaker:ListPipelineExecutions",
        "sagemaker:ListPipelineExecutionSteps",
        "sagemaker:UpdatePipeline",
        "sagemaker:DeletePipeline"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::ACCOUNT-ID:role/EXECUTION-ROLE-NAME",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "sagemaker.amazonaws.com"
        }
      }
    }
  ]
}
```

Replace `ACCOUNT-ID` and `EXECUTION-ROLE-NAME` with your actual values.

{% hint style="info" %}
ZenML will build a Docker image called `<CONTAINER_REGISTRY_URI>/zenml:<PIPELINE_NAME>` which includes your code and use it to run your pipeline steps in Sagemaker. Check out [this page](https://docs.zenml.io/how-to/customize-docker-builds/) if you want to learn more about how ZenML builds these images and how you can customize them.
{% endhint %}

You can now run any ZenML pipeline using the Sagemaker orchestrator:

```shell
python run.py
```

If all went well, you should now see the following output:

```
Steps can take 5-15 minutes to start running when using the Sagemaker Orchestrator.
Your orchestrator 'sagemaker' is running remotely. Note that the pipeline run 
will only show up on the ZenML dashboard once the first step has started 
executing on the remote infrastructure.
```

{% hint style="warning" %}
If it is taking more than 15 minutes for your run to show up, it might be that a setup error occurred in SageMaker before the pipeline could be started. Checkout the [Debugging SageMaker Pipelines](sagemaker.md#debugging-sagemaker-pipelines) section for more information on how to debug this.
{% endhint %}

### Sagemaker UI

Sagemaker comes with its own UI that you can use to find further details about your pipeline runs, such as the logs of your steps.

To access the Sagemaker Pipelines UI, you will have to launch Sagemaker Studio via the AWS Sagemaker UI. Make sure that you are launching it from within your desired AWS region.

![Sagemaker Studio launch](../../.gitbook/assets/sagemaker-studio-launch.png)

Once the Studio UI has launched, click on the 'Pipeline' button on the left side. From there you can view the pipelines that have been launched via ZenML:

![Sagemaker Studio Pipelines](../../.gitbook/assets/sagemakerUI.png)

### Debugging SageMaker Pipelines

If your SageMaker pipeline encounters an error before the first ZenML step starts, the ZenML run will not appear in the ZenML dashboard. In such cases, use the [SageMaker UI](sagemaker.md#sagemaker-ui) to review the error message and logs. Here's how:

* Open the corresponding pipeline in the SageMaker UI as shown in the [SageMaker UI Section](sagemaker.md#sagemaker-ui),
* Open the execution,
* Click on the failed step in the pipeline graph,
* Go to the 'Output' tab to see the error message or to 'Logs' to see the logs.

![SageMaker Studio Logs](../../.gitbook/assets/sagemaker-logs.png)

Alternatively, for a more detailed view of log messages during SageMaker pipeline executions, consider using [Amazon CloudWatch](https://aws.amazon.com/cloudwatch/):

* Search for 'CloudWatch' in the AWS console search bar.
* Navigate to 'Logs > Log groups.'
* Open the '/aws/sagemaker/ProcessingJobs' log group.
* Here, you can find log streams for each step of your SageMaker pipeline executions.

![SageMaker CloudWatch Logs](../../.gitbook/assets/sagemaker-cloudwatch-logs.png)

### Configuration at pipeline or step level

When running your ZenML pipeline with the Sagemaker orchestrator, the configuration set when configuring the orchestrator as a ZenML component will be used by default. However, it is possible to provide additional configuration at the pipeline or step level. This allows you to run whole pipelines or individual steps with alternative configurations. For example, this allows you to run the training process with a heavier, GPU-enabled instance type, while running other steps with lighter instances.

Additional configuration for the Sagemaker orchestrator can be passed via `SagemakerOrchestratorSettings`. Here, it is possible to configure `processor_args`, which is a dictionary of arguments for the Processor. For available arguments, see the [Sagemaker documentation](https://sagemaker.readthedocs.io/en/stable/api/training/processing.html#sagemaker.processing.Processor) . Currently, it is not possible to provide custom configuration for the following attributes:

* `image_uri`
* `instance_count`
* `sagemaker_session`
* `entrypoint`
* `base_job_name`
* `environment`

For example, settings can be provided and applied in the following way:

```python
from zenml import step
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import (
    SagemakerOrchestratorSettings
)

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    instance_type="ml.m5.large",
    volume_size_in_gb=30,
    environment={"MY_ENV_VAR": "my_value"}
)


@step(settings={"orchestrator": sagemaker_orchestrator_settings})
def my_step() -> None:
    pass
```

For example, if your ZenML component is configured to use `ml.c5.xlarge` with 400GB additional storage by default, all steps will use it except for the step above, which will use `ml.t3.medium` (for Processing Steps) or `ml.m5.xlarge` (for Training Steps) with 30GB additional storage. See the next section for details on how ZenML decides which Sagemaker Step type to use.

Check out [this docs page](https://docs.zenml.io/concepts/steps_and_pipelines/configuration) for more information on how to specify settings in general.

For more information and a full list of configurable attributes of the Sagemaker orchestrator, check out the [SDK Docs](https://sdkdocs.zenml.io/latest/integration_code_docs/integrations-aws.html#zenml.integrations.aws) .

### Using Warm Pools for your pipelines

[Warm Pools in SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/train-warm-pools.html) can significantly reduce the startup time of your pipeline steps, leading to faster iterations and improved development efficiency. This feature keeps compute instances in a "warm" state, ready to quickly start new jobs.

To enable Warm Pools, use the [`SagemakerOrchestratorSettings`](https://sdkdocs.zenml.io/latest/integration_code_docs/integrations-aws.html#zenml.integrations.aws) class:

```python
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import SagemakerOrchestratorSettings

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    keep_alive_period_in_seconds = 300, # 5 minutes, default value
)
```

This configuration keeps instances warm for 5 minutes after each job completes, allowing subsequent jobs to start faster if initiated within this timeframe. The reduced startup time can be particularly beneficial for iterative development processes or frequently run pipelines.

If you prefer not to use Warm Pools, you can explicitly disable them:

```python
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import SagemakerOrchestratorSettings

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    keep_alive_period_in_seconds = None,
)
```

By default, the SageMaker orchestrator uses Training Steps where possible, which can offer performance benefits and better integration with SageMaker's training capabilities. To disable this behavior:

```python
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import SagemakerOrchestratorSettings

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    use_training_step = False
)
```

These settings allow you to fine-tune your SageMaker orchestrator configuration, balancing between faster startup times with Warm Pools and more control over resource usage. By optimizing these settings, you can potentially reduce overall pipeline runtime and improve your development workflow efficiency.

#### S3 data access in ZenML steps

In Sagemaker jobs, it is possible to [access data that is located in S3](https://docs.aws.amazon.com/sagemaker/latest/dg/model-access-training-data.html). Similarly, it is possible to write data from a job to a bucket. The ZenML Sagemaker orchestrator supports this via the `SagemakerOrchestratorSettings` and hence at component, pipeline, and step levels.

**Import: S3 -> job**

Importing data can be useful when large datasets are available in S3 for training, for which manual copying can be cumbersome. Sagemaker supports `File` (default) and `Pipe` mode, with which data is either fully copied before the job starts or piped on the fly. See the Sagemaker documentation referenced above for more information about these modes.

Note that data import and export can be used jointly with `processor_args` for maximum flexibility.

A simple example of importing data from S3 to the Sagemaker job is as follows:

```python
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import (
    SagemakerOrchestratorSettings
)

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    input_data_s3_mode="File",
    input_data_s3_uri="s3://some-bucket-name/folder"
)
```

In this case, data will be available at `/opt/ml/processing/input/data` within the job.

It is also possible to split your input over channels. This can be useful if the dataset is already split in S3, or maybe even located in different buckets.

```python
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import (
    SagemakerOrchestratorSettings
)

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    input_data_s3_mode="File",
    input_data_s3_uri={
        "train": "s3://some-bucket-name/training_data",
        "val": "s3://some-bucket-name/validation_data",
        "test": "s3://some-other-bucket-name/testing_data"
    }
)
```

Here, the data will be available in `/opt/ml/processing/input/data/train`, `/opt/ml/processing/input/data/val` and `/opt/ml/processing/input/data/test`.

In the case of using `Pipe` for `input_data_s3_mode`, a file path specifying the pipe will be available as per the description written [here](https://docs.aws.amazon.com/sagemaker/latest/dg/model-access-training-data.html#model-access-training-data-input-modes) . An example of using this pipe file within a Python script can be found [here](https://github.com/aws/amazon-sagemaker-examples/blob/main/advanced_functionality/pipe_bring_your_own/train.py) .

**Export: job -> S3**

Data from within the job (e.g. produced by the training process, or when preprocessing large data) can be exported as well. The structure is highly similar to that of importing data. Copying data to S3 can be configured with `output_data_s3_mode`, which supports `EndOfJob` (default) and `Continuous`.

In the simple case, data in `/opt/ml/processing/output/data` will be copied to S3 at the end of a job:

```python
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import (
    SagemakerOrchestratorSettings
)

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    output_data_s3_mode="EndOfJob",
    output_data_s3_uri="s3://some-results-bucket-name/results"
)
```

In a more complex case, data in `/opt/ml/processing/output/data/metadata` and `/opt/ml/processing/output/data/checkpoints` will be written away continuously:

```python
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import (
    SagemakerOrchestratorSettings
)

sagemaker_orchestrator_settings = SagemakerOrchestratorSettings(
    output_data_s3_mode="Continuous",
    output_data_s3_uri={
        "metadata": "s3://some-results-bucket-name/metadata",
        "checkpoints": "s3://some-results-bucket-name/checkpoints"
    }
)
```

{% hint style="warning" %}
Using multichannel output or output mode except `EndOfJob` will make it impossible to use TrainingStep and also Warm Pools. See corresponding section of this document for details.
{% endhint %}

### Tagging SageMaker Pipeline Executions and Jobs

The SageMaker orchestrator allows you to add tags to your pipeline executions and individual jobs. Here's how you can apply tags at both the pipeline and step levels:

```python
from zenml import pipeline, step
from zenml.integrations.aws.flavors.sagemaker_orchestrator_flavor import (
    SagemakerOrchestratorSettings
)

# Define settings for the pipeline
pipeline_settings = SagemakerOrchestratorSettings(
    pipeline_tags={
        "project": "my-ml-project",
        "environment": "production",
    }
)

# Define settings for a specific step
step_settings = SagemakerOrchestratorSettings(
    tags={
        "step": "data-preprocessing",
        "owner": "data-team"
    }
)

@step(settings={"orchestrator": step_settings})
def preprocess_data():
    # Your preprocessing code here
    pass

@pipeline(settings={"orchestrator": pipeline_settings})
def my_training_pipeline():
    preprocess_data()
    # Other steps...

# Run the pipeline
my_training_pipeline()
```

In this example:

* The `pipeline_tags` are applied to the entire SageMaker pipeline object. SageMaker automatically applies the pipeline\_tags to all its associated jobs.
* The `tags` in `step_settings` are applied to the specific SageMaker job for the `preprocess_data` step.

This approach allows for more granular tagging, giving you flexibility in how you categorize and manage your SageMaker resources. You can view and manage these tags in the AWS Management Console, CLI, or API calls related to your SageMaker resources.

### Enabling CUDA for GPU-backed hardware

Note that if you wish to use this orchestrator to run steps on a GPU, you will need to follow [the instructions on this page](https://docs.zenml.io/user-guides/tutorial/distributed-training/) to ensure that it works. It requires adding some extra settings customization and is essential to enable CUDA for the GPU to give its full acceleration.

### Scheduling Pipelines

The SageMaker orchestrator supports running pipelines on a schedule using SageMaker's native scheduling capabilities. You can configure schedules in three ways:

* Using a cron expression
* Using a fixed interval
* Running once at a specific time

```python
from datetime import datetime, timedelta

from zenml import pipeline
from zenml.config.schedule import Schedule

# Using a cron expression (runs every 5 minutes)
@pipeline
def my_scheduled_pipeline():
    # Your pipeline steps here
    pass

my_scheduled_pipeline.with_options(
    schedule=Schedule(cron_expression="0/5 * * * ? *")
)()

# Using an interval (runs every 2 hours)
@pipeline
def my_interval_pipeline():
    # Your pipeline steps here
    pass

my_interval_pipeline.with_options(
    schedule=Schedule(
        start_time=datetime.now(),
        interval_second=timedelta(hours=2)
    )
)()

# Running once at a specific time
@pipeline
def my_one_time_pipeline():
    # Your pipeline steps here
    pass

my_one_time_pipeline.with_options(
    schedule=Schedule(run_once_start_time=datetime(2024, 12, 31, 23, 59))
)()
```

When you deploy a scheduled pipeline, ZenML will:

1. Create a SageMaker Pipeline Schedule with the specified configuration
2. Configure the pipeline as the target for the schedule
3. Enable automatic execution based on the schedule

{% hint style="info" %}
If you run the same pipeline with a schedule multiple times, the existing schedule will **not** be updated with the new settings. Rather, ZenML will create a new SageMaker pipeline and attach a new schedule to it. The user must manually delete the old pipeline and their attached schedule using the AWS CLI or API (`aws scheduler delete-schedule <SCHEDULE_NAME>`). See details here: [SageMaker Pipeline Schedules](https://docs.aws.amazon.com/sagemaker/latest/dg/pipeline-eventbridge.html)
{% endhint %}

#### Required IAM Permissions for schedules

When using scheduled pipelines, you need to ensure your IAM role has the correct permissions and trust relationships. You can set this up by either defining an explicit `scheduler_role` in your orchestrator configuration or you can adjust the role that you are already using on the client side to manage Sagemaker pipelines.

```bash
# When registering the orchestrator
zenml orchestrator register sagemaker-orchestrator \
    --flavor=sagemaker \
    --scheduler_role=arn:aws:iam::123456789012:role/my-scheduler-role

# Or updating an existing orchestrator
zenml orchestrator update sagemaker-orchestrator \
    --scheduler_role=arn:aws:iam::123456789012:role/my-scheduler-role
```

{% hint style="info" %}
The IAM role that you are using on the client side can come from multiple sources depending on how you configured your orchestrator, such as explicit credentials, a service connector or an implicit authentication.

If you are using a service connector, keep in mind, this only works with authentication methods that involve IAM roles (IAM role, Implicit authentication). LINK
{% endhint %}

This is particularly useful when:

* You want to use different roles for creating pipelines and scheduling them
* Your organization's security policies require separate roles for different operations
* You need to grant specific permissions only to the scheduling operations

1.  **Trust Relationships** Your `scheduler_role` (or your client role if you did not configure a `scheduler_role`) needs to be assumed by the EventBridge Scheduler service:

    ```json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": {
            "AWS": "<ROLE_ARN>",
            "Service": [
              "scheduler.amazonaws.com"
            ]
          },
          "Action": "sts:AssumeRole"
        }
      ]
    }
    ```
2.  **Required IAM Permissions for the client role**

    In addition to permissions needed to manage pipelines, the role on the client side also needs the following permissions to create schedules on EventBridge:

    ```json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Action": [
            "scheduler:ListSchedules",
            "scheduler:GetSchedule",
            "scheduler:CreateSchedule",
            "scheduler:UpdateSchedule",
            "scheduler:DeleteSchedule"
          ],
          "Resource": "*"
        },
        {
          "Effect": "Allow",
          "Action": "iam:PassRole",
          "Resource": "arn:aws:iam::*:role/*",
          "Condition": {
            "StringLike": {
              "iam:PassedToService": "scheduler.amazonaws.com"
            }
          }
        }
      ]
    }
    ```

    Or you can use the `AmazonEventBridgeSchedulerFullAccess` managed policy.

    These permissions enable:

    * Creation and management of Pipeline Schedules
    * Setting up trust relationships between services
    * Managing IAM policies required for the scheduled execution
    * Cleanup of resources when schedules are removed

    Without these permissions, the scheduling functionality will fail. Make sure to configure them before attempting to use scheduled pipelines.
3.  **Required IAM Permissions for the `scheduler_role`**

    The `scheduler_role` requires the same permissions as the client role (that would run the pipeline in a non-scheduled case) to launch and manage SageMaker jobs. Use the same custom client permissions policy shown in the [Required IAM Permissions](#required-iam-permissions) section above instead of the broad `AmazonSageMakerFullAccess` managed policy.

<figure><img src="https://static.scarf.sh/a.png?x-pxid=f0b4f458-0a54-4fcd-aa95-d5ee424815bc" alt="ZenML Scarf"><figcaption></figcaption></figure>
