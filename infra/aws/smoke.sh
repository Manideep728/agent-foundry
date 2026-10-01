#!/bin/sh
# Deploy the fan-out to AWS, measure the suite through it, prove the result,
# and destroy everything. Run from a clean checkout:
#
#   AF_ALERT_EMAIL=you@example.com sh infra/aws/smoke.sh 2>&1 | tee smoke.log
#
# Needs AWS credentials (`aws sts get-caller-identity` must work), Terraform,
# Docker, and the runner image (`make runner-image`). Set AF_KEEP=1 to leave
# the deployment up; destroy it later with `terraform -chdir=infra/aws destroy`.
#
# What it proves, in order: the images run on ECS; every dispatched run reaches
# the RDS database exactly once, even though one worker is stopped partway;
# each run's files are in S3; and afterwards nothing is left running.
set -eu

: "${AF_ALERT_EMAIL:?set AF_ALERT_EMAIL, the address the budget alert emails}"
REGION=${AF_REGION:-us-east-1}
REPEATS=${AF_REPEATS:-2}
ROOT=$(git rev-parse --show-toplevel)
AWS_DIR="$ROOT/infra/aws"
TF="terraform -chdir=$AWS_DIR"

step() { printf '\n== %s (%s)\n' "$1" "$(date -u +%H:%M:%SZ)"; }

if ! git -C "$ROOT" diff --quiet HEAD --; then
    echo "smoke: commit first; the images are built from HEAD" >&2
    exit 1
fi
TAG=$(git -C "$ROOT" rev-parse --short=12 HEAD)
export TF_VAR_region="$REGION" TF_VAR_alert_email="$AF_ALERT_EMAIL" \
    TF_VAR_image_tag="$TAG" AWS_DEFAULT_REGION="$REGION"

step "identity"
aws sts get-caller-identity --query Arn --output text

step "registries"
$TF init -input=false >/dev/null
$TF apply -input=false -auto-approve \
    -target=aws_ecr_repository.runner -target=aws_ecr_repository.control_plane
REGISTRY=$($TF output -raw registry)
RUNNER_REPO=$($TF output -raw runner_repository)
CONTROL_REPO=$($TF output -raw control_plane_repository)

step "images, tagged $TAG"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
make -C "$ROOT" control-plane-image CONTROL_IMAGE="af-control-plane:$TAG"
docker tag afrunner:local "$RUNNER_REPO:$TAG"
docker tag "af-control-plane:$TAG" "$CONTROL_REPO:$TAG"
docker push --quiet "$RUNNER_REPO:$TAG"
docker push --quiet "$CONTROL_REPO:$TAG"

step "everything else"
$TF apply -input=false -auto-approve
CLUSTER=$($TF output -raw cluster)
LOGS=$($TF output -raw log_group)
ARTIFACTS=$($TF output -raw artifacts)
TOPICS=$($TF output -json one_off_tasks | python3 -c 'import json,sys; print(json.load(sys.stdin)["topics"])')
DISPATCH=$($TF output -json one_off_tasks | python3 -c 'import json,sys; print(json.load(sys.stdin)["dispatch"])')
REPORT=$($TF output -json one_off_tasks | python3 -c 'import json,sys; print(json.load(sys.stdin)["report"])')
SERVICES="kafka collector worker-w1 worker-w2"

# One-off task: start it, wait for it to stop, print its log, fail on non-zero.
run_task() {
    definition=$1
    overrides=${2:-'{}'}
    task=$(aws ecs run-task --cluster "$CLUSTER" --launch-type EC2 \
        --task-definition "$definition" --overrides "$overrides" \
        --query 'tasks[0].taskArn' --output text)
    aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task"
    name=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
        --query 'tasks[0].containers[0].name' --output text)
    code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
        --query 'tasks[0].containers[0].exitCode' --output text)
    aws logs get-log-events --log-group-name "$LOGS" \
        --log-stream-name "af/$name/${task##*/}" --start-from-head \
        --query 'events[].message' --output text | tr '\t' '\n'
    [ "$code" = "0" ] || { echo "smoke: $name exited $code" >&2; return 1; }
}

step "waiting for the instance and the broker"
until [ "$(aws ecs describe-clusters --clusters "$CLUSTER" \
    --query 'clusters[0].registeredContainerInstancesCount' --output text)" = "1" ]; do
    sleep 10
done
# The broker must be up before the topics can be created; the services that
# need the topics restart until they exist.
until run_task "$TOPICS" >/dev/null 2>&1; do sleep 15; done
run_task "$TOPICS"
aws ecs wait services-stable --cluster "$CLUSTER" --services $SERVICES

step "dispatch: 8 tasks x $REPEATS"
run_task "$DISPATCH" "{\"containerOverrides\":[{\"name\":\"dispatch\",\"command\":[\"dispatch\",\"--agent\",\"fake\",\"--image\",\"$RUNNER_REPO:$TAG\",\"--repeats\",\"$REPEATS\"]}]}" \
    | tee /tmp/af-smoke-dispatched.txt
# A dispatched run is a line that starts with a run id: 20261001-042344-3063c3de.
DISPATCHED=$(grep -cE '^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}' /tmp/af-smoke-dispatched.txt || true)

step "stopping worker-w1 mid-suite"
sleep 5
W1=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name worker-w1 \
    --query 'taskArns[0]' --output text)
aws ecs stop-task --cluster "$CLUSTER" --task "$W1" \
    --reason "smoke: redelivery check" --query 'task.lastStatus' --output text

step "waiting until all $DISPATCHED runs are in RDS"
for attempt in $(seq 1 40); do
    stored=$(run_task "$REPORT" 2>/dev/null |
        sed -n 's/^\([0-9][0-9]*\) run(s) recorded.*/\1/p' | head -n 1)
    echo "stored ${stored:-0} of $DISPATCHED"
    [ "${stored:-0}" = "$DISPATCHED" ] && break
    sleep 20
done

step "the report, read from RDS"
run_task "$REPORT" | sed -n '1,40p'

step "the collector's log"
aws logs filter-log-events --log-group-name "$LOGS" \
    --log-stream-name-prefix af/collector --filter-pattern '"af:"' \
    --query 'events[].message' --output text | tr '\t' '\n' | tail -n 5

step "artifacts in S3"
aws s3 ls "$ARTIFACTS/runs/" | head -n 3
aws s3 ls "$ARTIFACTS/runs/" | wc -l
FIRST=$(aws s3 ls "$ARTIFACTS/runs/" | awk 'NR==1 {print $2}')
aws s3 ls --recursive "$ARTIFACTS/runs/$FIRST"

if [ "${AF_KEEP:-0}" = "1" ]; then
    echo "smoke: AF_KEEP=1, leaving the deployment up"
    exit 0
fi

step "destroy"
$TF destroy -input=false -auto-approve | tail -n 1

step "nothing left"
aws ecs list-clusters --query 'clusterArns' --output text
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier' --output text
aws ec2 describe-instances --filters Name=tag:Project,Values=agentfoundry \
    Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[].Instances[].InstanceId' --output text
aws ecr describe-repositories --query 'repositories[].repositoryName' --output text
aws s3 ls | grep agentfoundry || echo "no agentfoundry bucket"
