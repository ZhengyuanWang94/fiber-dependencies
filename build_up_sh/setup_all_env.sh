#!/bin/bash

git clone git@github.com:fiberx/fiber.git
cd fiber
git checkout 1fdca443cb
git pull

echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
echo "setting up angr env..."
./setup_angr_env.sh

echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
echo "installing dep pkgs..."
./install_pkgs.sh

# if you found scripts not work,
# do
#====================================
# now you should in dir fiber/ 

#echo ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>"
# echo "fixing dependencies..."
# git clone git@github.com:ZhengyuanWang94/fiber-dependencies.git
# rm -rf setup_angr_env.sh install_pkgs.sh ./angr-dev/setup.sh
# mv ./fiber-dependencies/setup_angr_env.sh ./
# mv ./fiber-dependencies/install_pkgs.sh ./
# mv ./fiber-deendencies/angr-dev-fiber/setup.sh ./angr-dev/
# ./setup_angr_env.sh
# ./install_pkgs.sh

#====================================

# if any of the dependencies not works,
# just copy from fiber-dependencies and paste, be relax

# AND, 
# before you use fiber, remeber to modify ext_sig.py
# ADDR2LINE = ‘/your/path/to/addr2line’
# usually, 
# ADDR2LINE = ‘/usr/bin/addr2line’