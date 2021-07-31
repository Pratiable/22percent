#!/bin/sh
#make virtual_environment
echo y | conda create -n ttpercent python=3.8 && conda activate ttpercent
#install dependency_packages
pip install -r requirements.txt
#make_database
python manage.py migrate
#find_server_IPAddress
echo "your_server_IP_address"
echo  | hostname -I
#runserver_finally
python manage.py runserver 0:8000