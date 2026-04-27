#!/bin/bash
set -e
rm -rf virtualenv
rm -f __deployer__.zip
virtualenv --without-pip virtualenv
pip install -r requirements.txt --target virtualenv/lib/python3.12/site-packages
