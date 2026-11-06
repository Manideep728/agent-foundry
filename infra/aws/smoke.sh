#!/bin/sh
# Deploy the fan-out to AWS, measure the suite through it, prove the result,
# and destroy everything. Run from a clean checkout:
#
#   AF_ALERT_EMAIL=you@example.com sh infra/aws/smoke.sh 2>&1 | tee smoke.log
#
# Needs AWS credentials (`aws sts get-caller-identity` must work), Terraform,
# Docker and uv. Set AF_KEEP=1 to leave the deployment up; destroy it later
# with `terraform -chdir=infra/aws destroy`.
#
# What it proves, in order: the images run on ECS; every dispatched run reaches
# the RDS database exactly once; each run's files are in S3; and afterwards
# nothing is left running. It also stops one worker after the dispatch, but
# with the fake agent the suite can finish first, so a pass does not prove
# redelivery.
#
# Every wait has a deadline. Whatever the exit, success or not, the script
# prints what it can about the deployment, then destroys it and checks that
# nothing is left: a failed smoke must not be a bill.
set -eu

: "${AF_ALERT_EMAIL:?set AF_ALERT_EMAIL, the address the budget alert emails}"
REGION=${AF_REGION:-us-east-1}
REPEATS=${AF_REPEATS:-2}
ROOT=$(git rev-parse --show-toplevel)
AWS_DIR="$ROOT/infra/aws"
TF="terraform -chdir=$AWS_DIR"
WORK=$(mktemp -d)
CLUSTER=""
LOGS=""

step() { printf '\n== %s (%s)\n' "$1" "$(date -u +%H:%M:%SZ)"; }
fail() { echo "smoke: $1" >&2; exit 1; }

# Run "$@" until it succeeds or `seconds` pass, sleeping `pause` between tries.
retry_for() {
    seconds=$1 pause=$2
    shift 2
    deadline=$(($(date +%s) + seconds))
    until "$@"; do
        [ "$(date +%s)" -lt "$deadline" ] || return 1
        sleep "$pause"
    done
}

if ! git -C "$ROOT" diff --quiet HEAD --; then
    fail "commit first; the images are built from HEAD"
fi
TAG=$(git -C "$ROOT" rev-parse --short=12 HEAD)
export TF_VAR_region="$REGION" TF_VAR_alert_email="$AF_ALERT_EMAIL" \
    TF_VAR_image_tag="$TAG" AWS_DEFAULT_REGION="$REGION" AWS_PAGER=""

# What a failure leaves behind, read before the destroy removes it.
diagnose() {
    [ -n "$CLUSTER" ] || return 0
    step "diagnostics"
    aws ecs describe-services --cluster "$CLUSTER" --services $SERVICES \
        --query 'services[].[serviceName,runningCount,events[0].message]' \
        --output text || true
    for task in $(aws ecs list-tasks --cluster "$CLUSTER" --desired-status STOPPED \
        --query 'taskArns[]' --output text 2>/dev/null); do
        aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
            --query 'tasks[0].[group,stoppedReason,containers[0].exitCode,containers[0].reason]' \
            --output text || true
    done
    if [ -n "$LOGS" ]; then
        echo "-- last task log lines"
        aws logs filter-log-events --log-group-name "$LOGS" \
            --start-time $((($(date +%s) - 3600) * 1000)) \
            --query 'events[-40:].[logStreamName,message]' --output text || true
    fi
    instance=$($TF output -raw instance_id 2>/dev/null || true)
    if [ -n "$instance" ]; then
        echo "-- instance console, af-user-data lines"
        aws ec2 get-console-output --instance-id "$instance" --latest \
            --query Output --output text 2>/dev/null | grep 'af-user-data' || true
    fi
}

destroy_all() {
    step "destroy"
    for attempt in 1 2 3; do
        if $TF destroy -input=false -auto-approve >"$WORK/destroy.log" 2>&1; then
            tail -n 1 "$WORK/destroy.log"
            break
        fi
        tail -n 20 "$WORK/destroy.log"
        [ "$attempt" = 3 ] && { echo "smoke: DESTROY FAILED, resources may still bill" >&2; return 1; }
        sleep 30
    done

    step "nothing left"
    left=$(
        aws ecs list-clusters --query 'clusterArns[]' --output text | grep agentfoundry || true
        aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier' --output text
        aws ec2 describe-instances --filters Name=tag:Project,Values=agentfoundry \
            Name=instance-state-name,Values=pending,running,stopping,stopped \
            --query 'Reservations[].Instances[].InstanceId' --output text
        aws ecr describe-repositories --query 'repositories[].repositoryName' --output text
        aws s3 ls | grep agentfoundry || true
    )
    if [ -n "$(echo "$left" | tr -d '[:space:]')" ]; then
        echo "smoke: STILL PRESENT after destroy:" >&2
        echo "$left" >&2
        return 1
    fi
    echo "nothing left"
}

on_exit() {
    code=$?
    trap - EXIT INT TERM
    # Cleanup runs to the end whatever fails inside it.
    set +e
    [ "$code" = 0 ] || diagnose
    if [ "${AF_KEEP:-0}" = "1" ]; then
        echo "smoke: AF_KEEP=1, leaving the deployment up"
    else
        destroy_all || code=1
    fi
    rm -rf "$WORK"
    echo "smoke: exit $code"
    exit "$code"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

step "identity"
aws sts get-caller-identity --query Arn --output text

step "registries"
$TF init -input=false >/dev/null
$TF apply -input=false -auto-approve \
    -target=aws_ecr_repository.runner -target=aws_ecr_repository.control_plane
# A targeted apply stores only the outputs its targets produce, so the
# registry is taken from a repository URL rather than its own output.
RUNNER_REPO=$($TF output -raw runner_repository)
CONTROL_REPO=$($TF output -raw control_plane_repository)
REGISTRY=${RUNNER_REPO%%/*}

step "images, tagged $TAG"
make -C "$ROOT" runner-image
make -C "$ROOT" control-plane-image CONTROL_IMAGE="af-control-plane:$TAG"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
# The repositories refuse to overwrite a tag, so an image already pushed by an
# earlier attempt is left alone.
push() {
    local_image=$1 repo=$2
    if aws ecr describe-images --repository-name "${repo#*/}" \
        --image-ids imageTag="$TAG" >/dev/null 2>&1; then
        echo "$repo:$TAG already pushed"
        return 0
    fi
    docker tag "$local_image" "$repo:$TAG"
    retry_for 300 10 docker push --quiet "$repo:$TAG" || fail "push of $repo:$TAG failed"
}
push afrunner:local "$RUNNER_REPO"
push "af-control-plane:$TAG" "$CONTROL_REPO"

step "everything else"
SERVICES="kafka collector worker-w1 worker-w2"
$TF apply -input=false -auto-approve
CLUSTER=$($TF output -raw cluster)
LOGS=$($TF output -raw log_group)
ARTIFACTS=$($TF output -raw artifacts)
DATABASE_URL=$($TF output -raw database_url)
task_definition() {
    $TF output -json one_off_tasks | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"
}
TOPICS=$(task_definition topics)
DISPATCH=$(task_definition dispatch)
REPORT=$(task_definition report)

# A task's log, once CloudWatch has all of it: the stream may appear late, and
# its events arrive after the task stops, so read until two reads agree.
task_log() {
    stream=$1 previous=-1
    : >"$WORK/log"
    for attempt in $(seq 1 12); do
        if aws logs get-log-events --log-group-name "$LOGS" --log-stream-name "$stream" \
            --start-from-head --query 'events[].message' --output text \
            >"$WORK/log" 2>/dev/null; then
            count=$(wc -c <"$WORK/log")
            [ "$count" = "$previous" ] && break
            previous=$count
        fi
        sleep 5
    done
    tr '\t' '\n' <"$WORK/log"
}

# One-off task: start it, wait for it to stop, print its log, fail on non-zero.
run_task() {
    definition=$1
    overrides=${2:-'{}'}
    task=$(aws ecs run-task --cluster "$CLUSTER" --launch-type EC2 \
        --task-definition "$definition" --overrides "$overrides" \
        --query 'tasks[0].taskArn' --output text)
    if [ -z "$task" ] || [ "$task" = "None" ]; then
        aws ecs run-task --cluster "$CLUSTER" --launch-type EC2 \
            --task-definition "$definition" --query 'failures' --output text >&2 || true
        echo "smoke: could not place $definition" >&2
        return 1
    fi
    aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task" || return 1
    set -- $(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
        --query 'tasks[0].containers[0].[name,exitCode]' --output text)
    name=$1 code=${2:-None}
    task_log "af/$name/${task##*/}"
    if [ "$code" != "0" ]; then
        aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" \
            --query 'tasks[0].[stoppedReason,containers[0].reason]' --output text >&2
        echo "smoke: $name exited $code" >&2
        return 1
    fi
}

registered() {
    [ "$(aws ecs describe-clusters --clusters "$CLUSTER" \
        --query 'clusters[0].registeredContainerInstancesCount' --output text)" = "1" ]
}
topics_created() { run_task "$TOPICS" >"$WORK/topics.log" 2>&1; }

step "waiting for the instance and the broker"
retry_for 900 10 registered || fail "no container instance joined $CLUSTER in 15 minutes"
# The broker must be up before the topics can be created; the services that
# need the topics restart until they exist.
retry_for 900 15 topics_created || { cat "$WORK/topics.log"; fail "topics not created in 15 minutes"; }
cat "$WORK/topics.log"
aws ecs wait services-stable --cluster "$CLUSTER" --services $SERVICES ||
    fail "services did not become stable"

step "dispatch: every task x $REPEATS"
run_task "$DISPATCH" "{\"containerOverrides\":[{\"name\":\"dispatch\",\"command\":[\"dispatch\",\"--agent\",\"fake\",\"--image\",\"$RUNNER_REPO:$TAG\",\"--repeats\",\"$REPEATS\"]}]}" \
    >"$WORK/dispatch.log" || { cat "$WORK/dispatch.log"; fail "dispatch failed"; }
cat "$WORK/dispatch.log"
# A dispatched run is a line that starts with a run id: 20261001-042344-3063c3de.
grep -oE '^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}' "$WORK/dispatch.log" | sort >"$WORK/dispatched" || true
DISPATCHED=$(wc -l <"$WORK/dispatched" | tr -d ' ')
ANNOUNCED=$(sed -n 's/.*dispatched \([0-9][0-9]*\) run(s).*/\1/p' "$WORK/dispatch.log")
[ "$DISPATCHED" -gt 0 ] || fail "dispatch printed no run ids"
[ "$DISPATCHED" = "${ANNOUNCED:-}" ] ||
    fail "dispatch announced ${ANNOUNCED:-nothing} run(s), but its log has $DISPATCHED ids"

step "stopping worker-w1 mid-suite"
sleep 5
W1=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name worker-w1 \
    --query 'taskArns[0]' --output text)
if [ -n "$W1" ] && [ "$W1" != "None" ]; then
    aws ecs stop-task --cluster "$CLUSTER" --task "$W1" \
        --reason "smoke: redelivery check" --query 'task.lastStatus' --output text
else
    echo "worker-w1 had no running task to stop"
fi

# Stored runs, one id per line, read through `af list` against RDS.
LIST="{\"containerOverrides\":[{\"name\":\"report\",\"command\":[\"list\",\"--db\",\"$DATABASE_URL\",\"--limit\",\"100000\"]}]}"
all_stored() {
    run_task "$REPORT" "$LIST" 2>/dev/null |
        grep -oE '^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}' | sort >"$WORK/stored" || true
    echo "stored $(wc -l <"$WORK/stored" | tr -d ' ') of $DISPATCHED"
    [ -z "$(comm -23 "$WORK/dispatched" "$WORK/stored")" ]
}

step "waiting until all $DISPATCHED runs are in RDS"
retry_for 1200 20 all_stored || {
    echo "missing:"
    comm -23 "$WORK/dispatched" "$WORK/stored"
    fail "not every dispatched run reached RDS in 20 minutes"
}
DUPLICATES=$(uniq -d "$WORK/stored")
[ -z "$DUPLICATES" ] || fail "runs stored more than once: $DUPLICATES"
EXTRA=$(comm -13 "$WORK/dispatched" "$WORK/stored")
[ -z "$EXTRA" ] || fail "runs stored that were never dispatched: $EXTRA"
echo "every dispatched run is stored exactly once"

step "the report, read from RDS"
run_task "$REPORT" | sed -n '1,40p'

step "the collector's log"
aws logs filter-log-events --log-group-name "$LOGS" \
    --log-stream-name-prefix af/collector --filter-pattern '"af:"' \
    --query 'events[].message' --output text | tr '\t' '\n' | tail -n 5

step "artifacts in S3"
aws s3 ls "$ARTIFACTS/runs/" | awk '{print $2}' | tr -d '/' | sort >"$WORK/uploaded" || true
MISSING=$(comm -23 "$WORK/dispatched" "$WORK/uploaded")
[ -z "$MISSING" ] || fail "no artifacts in S3 for: $MISSING"
echo "artifacts for all $DISPATCHED runs"
FIRST=$(head -n 1 "$WORK/dispatched")
aws s3 ls --recursive "$ARTIFACTS/runs/$FIRST/"

step "smoke passed"
