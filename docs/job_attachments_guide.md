---
title: Job Attachments Guide
---

# User perspective of job attachments

Job attachments provide a simple way to send files to your Deadline Cloud farm for processing,
then download output files when those jobs complete. Farms with [storage profiles] configured
for shared file systems can use job attachments to attach auxiliary files from local drives.
You can test Deadline Cloud in your environment with little infrastructure setup because
job attachments make the following equivalent:

* Running the job locally on your workstation with the [openjd run] command.
* Submitting the job to Deadline Cloud with the [deadline bundle submit] command,
  letting the job complete, then downloading the output with either the [deadline queue sync-output]
  or [deadline job download-output] command.

Job attachments use S3 for storage, which is generally lower cost than deploying your own file system
servers in a VPC. Deadline Cloud avoids re-uploading previously attached files, so repeated submissions
only upload new or modified files. This optimizes iterative workloads, like rendering
a scene that references static textures and geometry, or using an input video to train
Gaussian splats with different model options.

Configure [automatic downloads] by running the [deadline queue sync-output] command
on a schedule on your workstation or an on-premises server. Each run downloads
new task outputs since the last run, making job output files available for review
soon after completion.

[storage profiles]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/storage-profile.html
[openjd run]: https://github.com/OpenJobDescription/openjd-cli?tab=readme-ov-file#run
[deadline bundle submit]: cli_reference/deadline_bundle.md#submit
[deadline queue sync-output]: cli_reference/deadline_queue.md#sync-output
[deadline job download-output]: cli_reference/deadline_job.md#download-output
[automatic downloads]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/auto-downloads.html

# Developer perspective of job attachments

As a developer building with Deadline Cloud, you can use job attachments to provide data
to jobs and retrieve output. One option is using job bundles to specify file data flow
just like end users do.

Another option is writing code that directly uses the components of job attachments:
manifests for directory snapshots, content-addressed S3 storage, and the manifest paths
stored in Deadline Cloud job and session action resources.

## Queue configuration of job attachments

To use job attachments, configure them on your Deadline Cloud queue:
1. Create an S3 bucket and choose the prefix for job attachments data.
2. Call the [UpdateQueue API] to set the `s3BucketName` and
   `rootPrefix` fields of `jobAttachmentSettings`.
3. Add an IAM policy to the queue role granting read/write access to that S3 bucket prefix.

Users assume the queue IAM role to upload or download job attachments data. You can apply
identical S3 bucket configurations to multiple queues for better caching when jobs share data,
or prevent cross-queue data access by isolating to different S3 buckets or prefixes.
See the [job attachments security] topic for best practices you can apply.

[UpdateQueue API]: https://docs.aws.amazon.com/deadline-cloud/latest/APIReference/API_UpdateQueue.html
[job attachments security]: https://docs.aws.amazon.com/deadline-cloud/latest/userguide/security-best-practices.html#job-attachment-queues

## Organization of the job attachments S3 prefix

Data for job attachments follows a specific structure on S3. The `Data/` prefix
contains content-addressed storage for all files attached to jobs.
Each file is an object named according to its 128-bit xxhash sum,
for example a text file with content "Hello world!" is stored as
`Data/f09af903556aabe8b7a8534c8dddaa13.xxh128`.
The xxhash function provides high performance and good statistical properties,
but cannot be used for security purposes as it is not a cryptographic hash.

The `Manifests/` prefix contains manifests referencing the files in `Data/`.
Asset manifests represent directory structures of files relative to an unspecified root.
While the client library uses a naming convention for manifest keys,
the source of truth for manifest keys is available by calling Deadline Cloud APIs.

## Manifests for job inputs and outputs

The S3 keys for input manifests can be found by calling [GetJob API] and checking 
`attachments.manifests[].rootPath` in the response.

Output manifest S3 keys are stored differently depending on worker agent version:
- Prior to version (TODO VERSION HERE): Outputs were stored per task following a specific naming convention
- Since version (TODO VERSION HERE): The same convention is used, but the source of truth is the 
  `manifests[]` list from the [GetSessionAction API]

This manifest list corresponds to the [GetJob API] manifest list. For additional properties 
like the asset root directory, use the corresponding entry from the GetJob response.

The naming convention for task output manifests is:
```
Manifests/<farm_id>/<queue_id>/<job_id>/<step_id>/<task_id>/<timestamp>_<session_action_id>/<manifest_hash>_output
```

Where:
- `<root_prefix>` is the prefix configured in the queue's job attachment settings
- `<farm_id>`, `<queue_id>`, `<job_id>`, `<step_id>`, `<task_id>`, and `<session_action_id>` are the respective identifiers (e.g., farm-1234567890abcdefg)
- `<manifest_hash>` is a hash of the concatenation of `fileSystemLocationName` (if set) and `rootPath` fields in the job's `manifests` list.
- `<timestamp>` is the time that the task started. It is formatted as an ISO8601 timestamp with microsecond precision and in the UTC timezone (e.g., `2025-04-01T17:27:28.044179Z`)

When the [GetSessionAction API] manifest list is unavailable, reconstruct it by
calculating `<manifest_hash>` values for each [GetJob API] manifest entry and matching against
the `<manifest_hash>` value from the manifest object key.

[GetJob API]: https://docs.aws.amazon.com/deadline-cloud/latest/APIReference/API_GetJob.html
[GetSessionAction API]: https://docs.aws.amazon.com/deadline-cloud/latest/APIReference/API_GetSessionAction.html

## Asset manifest format

Asset manifests are JSON documents with the following schema.

```json
{
    "manifestVersion": "2023-03-03",
    "hashAlg": "xxh128",
    "totalSize": 12345,
    "paths": [
        {
            "path": "relative/path/to/file1.txt",
            "hash": "abcdef1234567890",
            "size": 1024,
            "mtime": 1678012345000000
        },
        {
            "path": "relative/path/to/file2.png",
            "hash": "0987654321fedcba",
            "size": 11321,
            "mtime": 1678012346000000
        }
    ]
}
```

The components of the manifest are:

- `manifestVersion`: The version of the manifest schema (currently "2023-03-03")
- `hashAlg`: The algorithm used to hash the files (currently only "xxh128" is supported)
- `totalSize`: The sum of all file sizes in bytes
- `paths`: An array of file entries, each containing:
  - `path`: The relative path to the file from the root directory
  - `hash`: The hash of the file contents using the specified algorithm
  - `size`: The file size in bytes
  - `mtime`: The file's last-modified time as epoch time in microseconds

Manifests use a canonical JSON representation, ensuring identical content for manifests
with matching directory structure, file contents, and mtime.

## Job input upload



## Job output download

## Job attachments on worker hosts

