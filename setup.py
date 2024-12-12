#!/usr/bin/env python
from setuptools import setup

with open('README.md', 'r') as fh:
    long_desc = fh.read()

setup(name='dz-mongodb',
      version='1.4.9',
      description='Singer.io tap for extracting data from MongoDB - Datazip compatible',
      long_description=long_desc,
      long_description_content_type='text/markdown',
      author='Wise',
      url='https://github.com/datazip/dz-mongodb',
      classifiers=[
          'Programming Language :: Python :: 3 :: Only',
          'Programming Language :: Python :: 3.6',
          'Programming Language :: Python :: 3.7',
          'Programming Language :: Python :: 3.8',
      ],
      py_modules=['dz_mongodb'],
      install_requires=[
          'pipelinewise-singer-python==1.*',
          'pymongo==4.3.*',
          'tzlocal==2.1.*',
          'terminaltables==3.1.*',
          'dnspython==2.1.*',
      ],
      extras_require={
          'dev': [
              'pylint==2.12',
              'ipdb==0.13.*'
          ],
          'test': [
              'pytest==6.2.5',
              'pytest-cov==3.0.0'
          ]
      },
      entry_points='''
          [console_scripts]
          dz-mongodb=dz_mongodb:main
      ''',
      packages=['dz_mongodb', 'dz_mongodb.sync_strategies'],
)
