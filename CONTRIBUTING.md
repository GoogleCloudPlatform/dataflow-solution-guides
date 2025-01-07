# How to Contribute

We'd love to accept your patches and contributions to this project. There are
just a few small guidelines you need to follow.

## Contributor License Agreement

Contributions to this project must be accompanied by a Contributor License
Agreement. You (or your employer) retain the copyright to your contribution;
this simply gives us permission to use and redistribute your contributions as
part of the project. Head over to <https://cla.developers.google.com/> to see
your current agreements on file or to sign a new one.

You generally only need to submit a CLA once, so if you've already submitted one
(even if it was for a different project), you probably don't need to do it
again.

## Run in Dataflow and Google Cloud

Before submitting your contribution, make sure that all your code runs correctly 
in Google Cloud, including any Terraform code and any pipeline you write.

## Code Quality Checks

### For Python code

You normally will write Python code in a subdirectory of the `pipelines` folder. 
Install `yapf` and run the following command in the top level directory of your
 pipeline, to reformat your code:

```shell
 yapf -i -r --style yapf .
 ```

 If you install `pylint`, you can check if your code will pass the build with the 
 following command:

 ```shell
pylint --rcfile ../pylintrc .
```

Please note that the configuration file `../pylintrc` is located in the
 `pipelines` folder.

 ### For Java code

 Make sure you are using Gradle with the same settings as the existing pipelines 
 (e.g. use `pipelines/etl_integration_java` as an example), and run the following 
 command to make your build passes:

 ```shell
 ./gradlew build
 ```

 If you find code style issues, run this command to fix them:

 ```
 shell
 ./gradlew spotlessApply
 ```

 You can use the following files to copy the Gradle settings to your pipeline:
 * `build.gradle`
 * `gradlew` and `gradlew.bat`
 * The directory `gradle` and all its contents.

 ### For Terraform code

 Run the following command in the top level directory where your Terraform code is located:

 ```shell
 terraform fmt
 ```

You can also check for other types of issues with your Terraform code by using the 
`terraform validate` command (but bear in mind you need to run `terraform init` command first).

## Code Reviews

All submissions, including submissions by project members, require review. We
use GitHub pull requests for this purpose. Consult
[GitHub Help](https://help.github.com/articles/about-pull-requests/) for more
information on using pull requests.

## Community Guidelines

This project follows [Google's Open Source Community
Guidelines](https:git//opensource.google/conduct/).

## Contributor Guide

If you are new to contributing to open source, you can find helpful information in this contributor guide.

You may follow these steps to contribute:

1. **Fork the official repository.** This will create a copy of the official repository in your own account.
2. **Sync the branches.** This will ensure that your copy of the repository is up-to-date with the latest changes from the official repository.
3. **Work on your forked repository's feature branch.** This is where you will make your changes to the code.
4. **Commit your updates on your forked repository's feature branch.** This will save your changes to your copy of the repository.
5. **Submit a pull request to the official repository's main branch.** This will request that your changes be merged into the official repository.
6. **Resolve any lint errors.** This will ensure that your changes are formatted correctly.

Here are some additional things to keep in mind during the process:

- **Read the [Google's Open Source Community Guidelines](https://opensource.google/conduct/).** The contribution guidelines will provide you with more information about the project and how to contribute.
- **Test your changes.** Before you submit a pull request, make sure that your changes work as expected.
- **Be patient.** It may take some time for your pull request to be reviewed and merged.
