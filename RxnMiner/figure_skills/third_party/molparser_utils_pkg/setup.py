from setuptools import setup, find_packages
setup(name="molparser_utils", version="1.0.0", packages=find_packages(),
      include_package_data=True, package_data={"molparser_utils": ["utils/*.csv"]})
