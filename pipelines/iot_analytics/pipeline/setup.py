from setuptools import setup, find_packages

setup(
    name='transformations',
    version='1.0',
    packages=find_packages(),
    install_requires=[
        'apache-beam[gcp]==2.59.0',
        'pandas',
        'scikit-learn'
    ],
    package_data={
        'transformations': ['maintenance_model.pkl']
    }
)