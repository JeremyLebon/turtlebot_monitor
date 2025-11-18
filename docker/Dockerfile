ARG BASE_IMAGE=osrf/ros:humble-desktop-full
FROM $BASE_IMAGE


RUN apt-get update

RUN apt-get install --no-install-recommends -y \
python3-argcomplete python3-colcon-common-extensions \
libboost-system-dev build-essential \
ros-humble-hls-lfcd-lds-driver \
ros-humble-turtlebot3-msgs \
ros-humble-dynamixel-sdk \
libudev-dev \
ros-humble-cartographer \
ros-humble-cartographer-ros \ 
ros-humble-navigation2 \
ros-humble-nav2-bringup \
ros-humble-urdf \
ros-humble-rmw-cyclonedds-cpp



#COPY turtlebot3 /root/turtlebot3_ws/src/turtlebot3

WORKDIR /root/turtlebot3_ws/src/turtlebot3

WORKDIR /root/turtlebot3_ws

COPY ./cyclonedds_config.xml /root/turtlebot3_ws/cylconedds_config.xml

RUN /bin/bash -c '. /opt/ros/${ROS_DISTRO}/setup.bash; colcon build --parallel-workers $(nproc) --cmake-args -DCMAKE_BUILD_TYPE=Release -DBUILD_WITH_CUDA=ON -DBUILD_WITH_VIEWER=ON'

RUN echo "source /ros_entrypoint.sh" >> /root/.bashrc  
# RUN echo 'export ROS_DOMAIN_ID=30 #TURTLEBOT3' >> /root/.bashrc  
# RUN echo 'export LDS_MODEL=LDS-01' >> /root/.bashrc  
RUN echo 'export TURTLEBOT3_MODEL=burger' >> /root/.bashrc
RUN echo "source /root/turtlebot3_ws/install/setup.bash" >> /root/.bashrc    

RUN apt-get update && apt-get install -y --no-install-recommends \
    nmap \
    avahi-utils \
    libnss-mdns \
    iproute2 \
    net-tools \
   python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Fix older wheel issues — USE pip3 instead of pip
RUN pip3 install --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip3 install  -r requirements.txt
# --no-cache-dir --no-build-isolation --prefer-binary -r requirements.txt
COPY app.py .
COPY app_V1.py .
COPY turtlebots.json .
COPY templates ./templates

EXPOSE 5000
CMD ["python", "app_V1.py"]