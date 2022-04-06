#!/usr/bin/env bash
set -xe

npm install -g configurable-http-proxy

# if you make updates here, please update the
# entries in ../dev-environment.yaml
pip install -U \
    aiohttp \
    "click<8.1.0" \
    colorlog \
    cryptography \
    dask \
    distributed \
    ipywidgets \
    jupyterhub \
    notebook \
    pytest \
    pytest-asyncio \
    sqlalchemy \
    tornado \
    traitlets \
    trustme

pushd dask-gateway
python setup.py develop
popd

pushd dask-gateway-server
python setup.py develop
popd

pip list

set +xe
