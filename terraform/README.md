# Deployment of the solution guides

In this directory, you will find all the Terraform code to spawn all the
necessary infrastructure in Google Cloud to deploy each one of the solution
guides.

Please refer to [the main documentation in this repo for a full list of all
the use cases](../README.md).

## Google Cloud security foundations

The deployments in this directory follow all the recommendations given in the 
[Google Cloud Security Foundations](https://cloud.google.com/architecture/security-foundations).

Some of the features of the Terraform deployments in this directory are the following:
* **Identity and Access Management (IAM):**
    * All resources are created with the minimum required permissions.
    * Service accounts are used for all deployments.
    * IAM policies are used to restrict access to resources.
* **Network security:**
    * All resources are deployed using private IPs only.
    * Firewalls are used to restrict access to resources, including network tags for `ssh`, `http-server` and `https-server` access.
    * If the project is created by the Terraform scripts, the default network is removed.
