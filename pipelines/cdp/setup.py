
from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requirements = f.readlines()

setup(
    name="Dataflow Solution for Customer Data Platform",
    version="0.1",
    description="A Customer Data Platform example for the Dataflow Solution Guides.",
    packages=find_packages(),
    install_requires=requirements,
)