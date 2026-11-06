#!/bin/bash
# Prepares the container instance before the ECS agent starts any task.
# Each step prints an `af-user-data:` line, which reaches the instance's
# console output, so a failed boot can be read after the fact.
set -euo pipefail

say() { echo "af-user-data: $1" | tee /dev/console; }

# Task roles for host-network tasks are off unless the agent is told; without
# them a worker would fall back to the instance role, which cannot write S3.
cat >>/etc/ecs/ecs.config <<EOF
ECS_CLUSTER=${cluster}
ECS_ENABLE_TASK_IAM_ROLE=true
ECS_ENABLE_TASK_IAM_ROLE_NETWORK_HOST=true
EOF
say "joined ${cluster}"

# A fixed id for the docker group, so a worker task can name it as its primary
# group and reach the socket without running as root. The socket is created
# with the group by name, so the change also holds after Docker restarts.
groupmod -g ${docker_gid} docker
systemctl start docker
chgrp docker /var/run/docker.sock
say "docker group is ${docker_gid}"

# Owned by the image's user, which every worker and runner runs as.
mkdir -p ${host_root}
chown 1000:${docker_gid} ${host_root}

# The worker starts the runner with `--pull never`, so the image has to be on
# the instance first. It is pushed before the instance is created; the retry
# covers a registry that is still settling.
command -v aws >/dev/null || dnf install -y awscli-2
for attempt in $(seq 1 20); do
    if aws ecr get-login-password --region ${region} |
        docker login --username AWS --password-stdin ${registry} &&
        docker pull ${runner_image}; then
        break
    fi
    sleep 15
done
if docker image inspect ${runner_image} >/dev/null 2>&1; then
    say "runner image present"
else
    say "runner image MISSING: ${runner_image}"
    exit 1
fi
