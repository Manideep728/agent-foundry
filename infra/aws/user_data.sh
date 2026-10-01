#!/bin/bash
# Prepares the container instance before the ECS agent starts any task.
set -euo pipefail

echo "ECS_CLUSTER=${cluster}" >>/etc/ecs/ecs.config

# A fixed id for the docker group, so a worker task can name it as its primary
# group and reach the socket without running as root. The socket is created
# with the group by name, so the change also holds after Docker restarts.
groupmod -g ${docker_gid} docker
chgrp docker /var/run/docker.sock

# Owned by the image's user, which every worker and runner runs as.
mkdir -p ${host_root}
chown 1000:${docker_gid} ${host_root}

# The worker starts the runner with `--pull never`, so the image has to be on
# the instance first. It is pushed before the instance is created; the retry
# covers a registry that is still settling.
aws ecr get-login-password --region ${region} |
    docker login --username AWS --password-stdin ${registry}
for attempt in $(seq 1 20); do
    docker pull ${runner_image} && break
    sleep 15
done
