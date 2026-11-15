# Troubleshooting

Operational notes: what a symptom means, and what to do about it. The README's
limitations are about what the measurements can claim; these are about running
the harness.

## A model call times out

`template-render` asks the model for a whole file, and a 3B model on an 8 GiB
GPU takes about 470 s to write it. Under the default 300 s client timeout the
run is recorded as `agent_error`, which is not a measurement. Raise the
timeout:

```
uv run af run tasks/template-render.toml --agent tools --ollama-timeout 900
```

`--ollama-timeout` applies to either provider. A worker's Kafka poll interval
follows it (D32), so a long call is not mistaken for a dead worker.

## Runs fail with "ollama reused N cached prompt tokens"

Every call starts from an unloaded model (D31), and a reply that still reports
reused prompt tokens fails the run. The usual cause is a second client of the
same Ollama server, which loaded the model between the unload and the request.
Give each worker its own server.

## A tool call is recorded as text

`llama3.2:3b` uses the `tool_calls` field. Both `qwen2.5-coder` models write the
call into the message body instead, and the 3B one sends several JSON objects
separated by newlines. The harness reads one JSON value written as text and
refuses anything else (D22), and the run's agent note counts the turns that did
this. Such runs under-report what the model asked for.

## A hosted tier keeps answering 429

The OpenAI-compatible client waits as `Retry-After` says, or 5 s doubling, for
at most 6 attempts and 60 s per wait (D38). A run that waited says so in its
agent note (`af show <run-id>`). A run that ran out of attempts is
`agent_error`. Run fewer tasks at a time, or a smaller suite, under the tier's
limit.

## A hosted provider answers 403 or 404 before any model runs

A 403 with `error code: 1010` comes from Cloudflare, not from the API: it
refused the client's signature. The client sends `User-Agent:
agentfoundry/<version>` for this reason, so a build older than that fix fails
here. A 404 that names the model usually means the provider no longer serves
it. Free tiers often remove models. List the models of the provider before a
run:

```
curl -s -H "Authorization: Bearer $GROQ_API_KEY" https://api.groq.com/openai/v1/models
```

## A stopped worker's container is still on the host

Each container carries the database that started it as a label, and recovery
removes only its own containers, so it never removes another worker's live
attempt of a redelivered run. A worker that is restarted with
the same `--db` removes what its previous run left. A worker that is never
restarted leaves its container behind; when no worker is running, remove them
all:

```
docker container prune --filter label=agentfoundry.run_id
```

## A real-repository task fails as `task_invalid`

The task's test patch did not apply to its own fixture, after the files it
touches were restored (D39). The fixture and the task disagree: run
`af fetch tasks/oss` again, which re-extracts a fixture whose hash differs from
the pin and refuses one that still differs.

## `af run` on a real-repository task says the fixture is missing

Real-repository fixtures are extracted from their images and are not in the
repository. Fetch them first, which needs the network and several GB per task:

```
uv run af fetch tasks/oss
```

They go to `~/.cache/agentfoundry/fixtures`, or `$AF_FIXTURE_CACHE`, and
`--oss-fixtures` names another directory.
