#!/bin/bash
set -e

# ONLY use this to test builds. Actual build should be done by github actions

BUILD_DIR=$(pwd)/build
MESSAGE="automatic build"
PACKAGE="pywebdriver"
BASE_IMAGE=ubuntu:18.04
KEY=

mkdir -p $BUILD_DIR

cat <<DEBUILD > debuild.sh
# 18.04 build uses Python 3.6, whose default text encoding in this environment is ASCII.
# In postelium package, there are some unicode characters in the description.
# Set LANG and LC_ALL to C.UTF-8 to avoid "UnicodeEncodeError: 'ascii' codec can't encode characters in position ..." errors
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

mkdir -p build
rm -f debian/changelog

# For weak/old CPUs which don't support AVX (e.g. Celeron J4125)
export DEB_CFLAGS_SET="-march=x86-64 -mtune=generic"
export DEB_CXXFLAGS_SET="-march=x86-64 -mtune=generic"
export CFLAGS="-march=x86-64 -mtune=generic"
export CXXFLAGS="-march=x86-64 -mtune=generic"

/pywebdriver/debian/pywebdriver/opt/venvs/pywebdriver/bin/pip install \
    "pip==21.3.1" \
    "setuptools==59.6.0" \
    "wheel==0.37.1"

/pywebdriver/debian/pywebdriver/opt/venvs/pywebdriver/bin/pip --version
/pywebdriver/debian/pywebdriver/opt/venvs/pywebdriver/bin/python -m pip show setuptools

dch --package $PACKAGE --newversion $(date +%Y%m%d) --create -m "$MESSAGE"
debuild
cp ../${PACKAGE}_* /build
DEBUILD
chmod +x debuild.sh

cat <<DOCKERFILE > Dockerfile
FROM $BASE_IMAGE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y gnupg ca-certificates
RUN apt-key adv --keyserver keyserver.ubuntu.com --recv-keys B249F40128AF28E09310254A4A8F432547238F7E
RUN echo "deb https://ppa.launchpadcontent.net/jyrki-pulliainen/dh-virtualenv/ubuntu bionic main" > /etc/apt/sources.list.d/dh-virtualenv.list
RUN apt-get update && apt-get install -y debhelper dh-python dh-virtualenv dh-systemd devscripts python3-wheel libcups2-dev python3-setuptools libmtp-dev python3-pip libffi-dev python3-venv python3-distutils python3-pip python3-simplejson python3-flask-babel python3-usb python3-serial python3-netifaces python3-cups python3-pillow
RUN python3 -m pip install flask-cors
#COPY . /$PACKAGE
WORKDIR /$PACKAGE
DOCKERFILE

read -p "Build Docker image? (y/n) " REPLY
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    docker pull $BASE_IMAGE
    docker build -t ${PACKAGE}_build .
fi

read -p "Buid $PACKAGE package? (y/n) " REPLY
echo
if [[ $REPLY =~ ^[Yy]$ ]]
then
    docker run -v $(pwd):/$PACKAGE -v $BUILD_DIR:/build -it ${PACKAGE}_build bash -c "./debuild.sh"
fi
