# AgentFoundry benchmark report

Generated 2026-11-15T10:17:17Z from `results.sqlite3`.

102 run(s) recorded, 99 of them measured, across 5 configuration(s).

Suites and configurations are reported separately and never averaged together. Counts are `passed/total` and durations are ranges, because every figure here is one some run actually had.

## synthetic -- fake/fake/scripted

24 measured of 24 attempted.

| measure | value |
|---|---|
| resolved | 24/24 |
| regressed | 0/24 |
| clean_patch | 24 |
| patch_with_regressions | 0 |
| tests_still_failing | 0 |
| validation_timeout | 0 |
| no_patch_produced | 0 |
| malformed_output | 0 |
| cost, micro-USD | 0 |
| prompt tokens, total | 100800 |
| output tokens, total | 7440 |
| tokens per run, min/median/max | 4510 / 4510 / 4510 |
| agent ms, min/median/max | 0 / 0 / 3 |
| validation ms, min/median/max | 533 / 550 / 860 |

- Architecture: x86_64
- Supervisor was PID 1: True
- Image: `sha256:41485236f78ec596568ee3a1f07fe144b3a8d1a31e92400f68e8b556dcc4ea6a`
- Harness: `0.1.0+a13bb7b30a76`

| task | outcome | resolved | regressed | fail->pass | pass->pass | agent ms | validation ms | cost |
|---|---|---|---|---|---|---|---|---|
| cache-eviction | clean_patch | yes | no | 1/1 | 1/1 | 3 | 840 | 0 |
| cache-eviction | clean_patch | yes | no | 1/1 | 1/1 | 1 | 535 | 0 |
| cache-eviction | clean_patch | yes | no | 1/1 | 1/1 | 0 | 548 | 0 |
| csv-quoted-fields | clean_patch | yes | no | 1/1 | 1/1 | 1 | 668 | 0 |
| csv-quoted-fields | clean_patch | yes | no | 1/1 | 1/1 | 0 | 543 | 0 |
| csv-quoted-fields | clean_patch | yes | no | 1/1 | 1/1 | 0 | 559 | 0 |
| interval-merge | clean_patch | yes | no | 1/1 | 1/1 | 0 | 610 | 0 |
| interval-merge | clean_patch | yes | no | 1/1 | 1/1 | 0 | 537 | 0 |
| interval-merge | clean_patch | yes | no | 1/1 | 1/1 | 0 | 573 | 0 |
| pagination-last-page | clean_patch | yes | no | 1/1 | 1/1 | 0 | 606 | 0 |
| pagination-last-page | clean_patch | yes | no | 1/1 | 1/1 | 0 | 574 | 0 |
| pagination-last-page | clean_patch | yes | no | 1/1 | 1/1 | 0 | 551 | 0 |
| path-normalise | clean_patch | yes | no | 1/1 | 1/1 | 0 | 543 | 0 |
| path-normalise | clean_patch | yes | no | 1/1 | 1/1 | 0 | 558 | 0 |
| path-normalise | clean_patch | yes | no | 1/1 | 1/1 | 0 | 533 | 0 |
| retry-backoff | clean_patch | yes | no | 1/1 | 1/1 | 1 | 540 | 0 |
| retry-backoff | clean_patch | yes | no | 1/1 | 1/1 | 0 | 561 | 0 |
| retry-backoff | clean_patch | yes | no | 1/1 | 1/1 | 0 | 547 | 0 |
| semver-compare | clean_patch | yes | no | 1/1 | 1/1 | 1 | 539 | 0 |
| semver-compare | clean_patch | yes | no | 1/1 | 1/1 | 0 | 860 | 0 |
| semver-compare | clean_patch | yes | no | 1/1 | 1/1 | 1 | 550 | 0 |
| template-render | clean_patch | yes | no | 1/1 | 1/1 | 0 | 545 | 0 |
| template-render | clean_patch | yes | no | 1/1 | 1/1 | 0 | 543 | 0 |
| template-render | clean_patch | yes | no | 1/1 | 1/1 | 0 | 545 | 0 |

8 of 8 task(s) agree across their repeats on result, patch and per-call tokens.

| task | runs | resolved | results | patches | token traces | agent ms, min/median/max | agrees |
|---|---|---|---|---|---|---|---|
| cache-eviction | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 1 / 3 | yes |
| csv-quoted-fields | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 0 / 1 | yes |
| interval-merge | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 0 / 0 | yes |
| pagination-last-page | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 0 / 0 | yes |
| path-normalise | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 0 / 0 | yes |
| retry-backoff | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 0 / 1 | yes |
| semver-compare | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 1 / 1 | yes |
| template-render | 3 | 3/3 | clean_patch | 1 | 1 | 0 / 0 / 0 | yes |

## synthetic -- single-shot/ollama/qwen2.5-coder:7b [single-shot-v1]

24 measured of 24 attempted.

| measure | value |
|---|---|
| resolved | 15/24 |
| regressed | 3/24 |
| clean_patch | 15 |
| patch_with_regressions | 3 |
| tests_still_failing | 6 |
| validation_timeout | 0 |
| no_patch_produced | 0 |
| malformed_output | 0 |
| cost, micro-USD | 0 |
| prompt tokens, total | 23586 |
| output tokens, total | 5772 |
| tokens per run, min/median/max | 714 / 1177 / 1858 |
| agent ms, min/median/max | 4635 / 6837 / 13918 |
| validation ms, min/median/max | 530 / 555 / 636 |

- Architecture: x86_64
- Supervisor was PID 1: True
- Image: `sha256:41485236f78ec596568ee3a1f07fe144b3a8d1a31e92400f68e8b556dcc4ea6a`
- Harness: `0.1.0+a13bb7b30a76`

| task | outcome | resolved | regressed | fail->pass | pass->pass | agent ms | validation ms | cost |
|---|---|---|---|---|---|---|---|---|
| cache-eviction | clean_patch | yes | no | 1/1 | 1/1 | 9951 | 593 | 0 |
| cache-eviction | clean_patch | yes | no | 1/1 | 1/1 | 7994 | 573 | 0 |
| cache-eviction | clean_patch | yes | no | 1/1 | 1/1 | 7890 | 562 | 0 |
| csv-quoted-fields | patch_with_regressions | no | yes | 1/1 | 0/1 | 5680 | 586 | 0 |
| csv-quoted-fields | patch_with_regressions | no | yes | 1/1 | 0/1 | 4635 | 560 | 0 |
| csv-quoted-fields | patch_with_regressions | no | yes | 1/1 | 0/1 | 4693 | 538 | 0 |
| interval-merge | clean_patch | yes | no | 1/1 | 1/1 | 6577 | 576 | 0 |
| interval-merge | clean_patch | yes | no | 1/1 | 1/1 | 6257 | 540 | 0 |
| interval-merge | clean_patch | yes | no | 1/1 | 1/1 | 6267 | 541 | 0 |
| pagination-last-page | clean_patch | yes | no | 1/1 | 1/1 | 5955 | 575 | 0 |
| pagination-last-page | clean_patch | yes | no | 1/1 | 1/1 | 5646 | 567 | 0 |
| pagination-last-page | clean_patch | yes | no | 1/1 | 1/1 | 5645 | 560 | 0 |
| path-normalise | clean_patch | yes | no | 1/1 | 1/1 | 6923 | 636 | 0 |
| path-normalise | clean_patch | yes | no | 1/1 | 1/1 | 6815 | 546 | 0 |
| path-normalise | clean_patch | yes | no | 1/1 | 1/1 | 6837 | 547 | 0 |
| retry-backoff | tests_still_failing | no | no | 0/1 | 1/1 | 7065 | 548 | 0 |
| retry-backoff | tests_still_failing | no | no | 0/1 | 1/1 | 7456 | 549 | 0 |
| retry-backoff | tests_still_failing | no | no | 0/1 | 1/1 | 6951 | 530 | 0 |
| semver-compare | clean_patch | yes | no | 1/1 | 1/1 | 13867 | 564 | 0 |
| semver-compare | clean_patch | yes | no | 1/1 | 1/1 | 13895 | 555 | 0 |
| semver-compare | clean_patch | yes | no | 1/1 | 1/1 | 13918 | 543 | 0 |
| template-render | tests_still_failing | no | no | 0/1 | 1/1 | 7016 | 535 | 0 |
| template-render | tests_still_failing | no | no | 0/1 | 1/1 | 6576 | 530 | 0 |
| template-render | tests_still_failing | no | no | 0/1 | 1/1 | 6597 | 551 | 0 |

8 of 8 task(s) agree across their repeats on result, patch and per-call tokens.

| task | runs | resolved | results | patches | token traces | agent ms, min/median/max | agrees |
|---|---|---|---|---|---|---|---|
| cache-eviction | 3 | 3/3 | clean_patch | 1 | 1 | 7890 / 7994 / 9951 | yes |
| csv-quoted-fields | 3 | 0/3 | patch_with_regressions | 1 | 1 | 4635 / 4693 / 5680 | yes |
| interval-merge | 3 | 3/3 | clean_patch | 1 | 1 | 6257 / 6267 / 6577 | yes |
| pagination-last-page | 3 | 3/3 | clean_patch | 1 | 1 | 5645 / 5646 / 5955 | yes |
| path-normalise | 3 | 3/3 | clean_patch | 1 | 1 | 6815 / 6837 / 6923 | yes |
| retry-backoff | 3 | 0/3 | tests_still_failing | 1 | 1 | 6951 / 7065 / 7456 | yes |
| semver-compare | 3 | 3/3 | clean_patch | 1 | 1 | 13867 / 13895 / 13918 | yes |
| template-render | 3 | 0/3 | tests_still_failing | 1 | 1 | 6576 / 6597 / 7016 | yes |

## synthetic -- tools/ollama/llama3.2:3b [tools-v2]

24 measured of 24 attempted.

| measure | value |
|---|---|
| resolved | 0/24 |
| regressed | 15/24 |
| clean_patch | 0 |
| patch_with_regressions | 15 |
| tests_still_failing | 0 |
| validation_timeout | 0 |
| no_patch_produced | 9 |
| malformed_output | 0 |
| cost, micro-USD | 0 |
| prompt tokens, total | 111690 |
| output tokens, total | 128622 |
| tokens per run, min/median/max | 2758 / 6829 / 43497 |
| agent ms, min/median/max | 7880 / 26054 / 462349 |
| validation ms, min/median/max | 545 / 564 / 604 |

- Architecture: x86_64
- Supervisor was PID 1: True
- Image: `sha256:41485236f78ec596568ee3a1f07fe144b3a8d1a31e92400f68e8b556dcc4ea6a`
- Harness: `0.1.0+a13bb7b30a76`

| task | outcome | resolved | regressed | fail->pass | pass->pass | agent ms | validation ms | cost |
|---|---|---|---|---|---|---|---|---|
| cache-eviction | no_patch_produced | no | no | 0/1 | 1/1 | 27341 | 576 | 0 |
| cache-eviction | no_patch_produced | no | no | 0/1 | 1/1 | 27127 | 564 | 0 |
| cache-eviction | no_patch_produced | no | no | 0/1 | 1/1 | 26417 | 545 | 0 |
| csv-quoted-fields | patch_with_regressions | no | yes | 0/1 | 0/1 | 13416 | 558 | 0 |
| csv-quoted-fields | patch_with_regressions | no | yes | 0/1 | 0/1 | 13403 | 589 | 0 |
| csv-quoted-fields | patch_with_regressions | no | yes | 0/1 | 0/1 | 13347 | 560 | 0 |
| interval-merge | patch_with_regressions | no | yes | 0/1 | 0/1 | 8931 | 548 | 0 |
| interval-merge | patch_with_regressions | no | yes | 0/1 | 0/1 | 7880 | 553 | 0 |
| interval-merge | patch_with_regressions | no | yes | 0/1 | 0/1 | 7941 | 562 | 0 |
| pagination-last-page | no_patch_produced | no | no | 0/1 | 1/1 | 26122 | 571 | 0 |
| pagination-last-page | no_patch_produced | no | no | 0/1 | 1/1 | 26054 | 554 | 0 |
| pagination-last-page | no_patch_produced | no | no | 0/1 | 1/1 | 26097 | 604 | 0 |
| path-normalise | patch_with_regressions | no | yes | 0/1 | 0/1 | 10470 | 566 | 0 |
| path-normalise | patch_with_regressions | no | yes | 0/1 | 0/1 | 10505 | 580 | 0 |
| path-normalise | patch_with_regressions | no | yes | 0/1 | 0/1 | 10383 | 571 | 0 |
| retry-backoff | patch_with_regressions | no | yes | 0/1 | 0/1 | 15343 | 564 | 0 |
| retry-backoff | patch_with_regressions | no | yes | 0/1 | 0/1 | 15005 | 555 | 0 |
| retry-backoff | patch_with_regressions | no | yes | 0/1 | 0/1 | 15079 | 547 | 0 |
| semver-compare | no_patch_produced | no | no | 0/1 | 1/1 | 26318 | 554 | 0 |
| semver-compare | no_patch_produced | no | no | 0/1 | 1/1 | 26306 | 576 | 0 |
| semver-compare | no_patch_produced | no | no | 0/1 | 1/1 | 26144 | 549 | 0 |
| template-render | patch_with_regressions | no | yes | 0/1 | 0/1 | 461363 | 570 | 0 |
| template-render | patch_with_regressions | no | yes | 0/1 | 0/1 | 462349 | 578 | 0 |
| template-render | patch_with_regressions | no | yes | 0/1 | 0/1 | 461249 | 562 | 0 |

8 of 8 task(s) agree across their repeats on result, patch and per-call tokens.

| task | runs | resolved | results | patches | token traces | agent ms, min/median/max | agrees |
|---|---|---|---|---|---|---|---|
| cache-eviction | 3 | 0/3 | no_patch_produced | 1 | 1 | 26417 / 27127 / 27341 | yes |
| csv-quoted-fields | 3 | 0/3 | patch_with_regressions | 1 | 1 | 13347 / 13403 / 13416 | yes |
| interval-merge | 3 | 0/3 | patch_with_regressions | 1 | 1 | 7880 / 7941 / 8931 | yes |
| pagination-last-page | 3 | 0/3 | no_patch_produced | 1 | 1 | 26054 / 26097 / 26122 | yes |
| path-normalise | 3 | 0/3 | patch_with_regressions | 1 | 1 | 10383 / 10470 / 10505 | yes |
| retry-backoff | 3 | 0/3 | patch_with_regressions | 1 | 1 | 15005 / 15079 / 15343 | yes |
| semver-compare | 3 | 0/3 | no_patch_produced | 1 | 1 | 26144 / 26306 / 26318 | yes |
| template-render | 3 | 0/3 | patch_with_regressions | 1 | 1 | 461249 / 461363 / 462349 | yes |

## oss -- fake/fake/scripted

15 measured of 15 attempted.

| measure | value |
|---|---|
| resolved | 15/15 |
| regressed | 0/15 |
| clean_patch | 15 |
| patch_with_regressions | 0 |
| tests_still_failing | 0 |
| validation_timeout | 0 |
| no_patch_produced | 0 |
| malformed_output | 0 |
| cost, micro-USD | 0 |
| prompt tokens, total | 63000 |
| output tokens, total | 4650 |
| tokens per run, min/median/max | 4510 / 4510 / 4510 |
| agent ms, min/median/max | 1 / 1 / 2 |
| validation ms, min/median/max | 594 / 2856 / 3720 |

- Architecture: x86_64
- Supervisor was PID 1: True
- Image: `sha256:014c3454c1f7031011cfaa954f7ee5377e2e85bbe35b8e0fb368dc1dc82acbb1`
- Image: `sha256:019c80e2ec574ac590b886eaf4291251480adcb6b3e249ed41f99eaff4eedb86`
- Image: `sha256:02e8bc7b38e71a685f242fffcd1cbf5a5f9d9677da3fb25bc67eefb01022fef7`
- Image: `sha256:bc301b25146744e069b4ff1bef1b5b5db469e5e9b0913b32378de3cf8bb77b3e`
- Image: `sha256:e8da2611f0a911aeb9c8f5442331bc9a01e130f1801309f648ac4422e7f3882c`
- Harness: `0.1.0+a13bb7b30a76`

| task | outcome | resolved | regressed | fail->pass | pass->pass | agent ms | validation ms | cost |
|---|---|---|---|---|---|---|---|---|
| pallets-flask-4045 | clean_patch | yes | no | 1/1 | 1/1 | 2 | 892 | 0 |
| pallets-flask-4045 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 758 | 0 |
| pallets-flask-4045 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 762 | 0 |
| pallets-flask-4992 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 3572 | 0 |
| pallets-flask-4992 | clean_patch | yes | no | 1/1 | 1/1 | 2 | 594 | 0 |
| pallets-flask-4992 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 624 | 0 |
| pallets-flask-5063 | clean_patch | yes | no | 1/1 | 1/1 | 2 | 3720 | 0 |
| pallets-flask-5063 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 696 | 0 |
| pallets-flask-5063 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 702 | 0 |
| pytest-dev-pytest-11143 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 3018 | 0 |
| pytest-dev-pytest-11143 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 2872 | 0 |
| pytest-dev-pytest-11143 | clean_patch | yes | no | 1/1 | 1/1 | 2 | 2856 | 0 |
| pytest-dev-pytest-11148 | clean_patch | yes | no | 1/1 | 1/1 | 2 | 3407 | 0 |
| pytest-dev-pytest-11148 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 3418 | 0 |
| pytest-dev-pytest-11148 | clean_patch | yes | no | 1/1 | 1/1 | 1 | 3424 | 0 |

5 of 5 task(s) agree across their repeats on result, patch and per-call tokens.

| task | runs | resolved | results | patches | token traces | agent ms, min/median/max | agrees |
|---|---|---|---|---|---|---|---|
| pallets-flask-4045 | 3 | 3/3 | clean_patch | 1 | 1 | 1 / 1 / 2 | yes |
| pallets-flask-4992 | 3 | 3/3 | clean_patch | 1 | 1 | 1 / 1 / 2 | yes |
| pallets-flask-5063 | 3 | 3/3 | clean_patch | 1 | 1 | 1 / 1 / 2 | yes |
| pytest-dev-pytest-11143 | 3 | 3/3 | clean_patch | 1 | 1 | 1 / 1 / 2 | yes |
| pytest-dev-pytest-11148 | 3 | 3/3 | clean_patch | 1 | 1 | 1 / 1 / 2 | yes |

## oss -- tools/ollama/llama3.2:3b [tools-v3]

12 measured of 15 attempted.

| measure | value |
|---|---|
| resolved | 0/12 |
| regressed | 0/12 |
| clean_patch | 0 |
| patch_with_regressions | 0 |
| tests_still_failing | 0 |
| validation_timeout | 0 |
| no_patch_produced | 12 |
| malformed_output | 0 |
| cost, micro-USD | 0 |
| prompt tokens, total | 166056 |
| output tokens, total | 61941 |
| tokens per run, min/median/max | 5410 / 23623 / 29971 |
| agent ms, min/median/max | 21470 / 57791 / 488348 |
| validation ms, min/median/max | 897 / 3358 / 4596 |

- Architecture: x86_64
- Supervisor was PID 1: True
- Image: `sha256:019c80e2ec574ac590b886eaf4291251480adcb6b3e249ed41f99eaff4eedb86`
- Image: `sha256:02e8bc7b38e71a685f242fffcd1cbf5a5f9d9677da3fb25bc67eefb01022fef7`
- Image: `sha256:bc301b25146744e069b4ff1bef1b5b5db469e5e9b0913b32378de3cf8bb77b3e`
- Image: `sha256:e8da2611f0a911aeb9c8f5442331bc9a01e130f1801309f648ac4422e7f3882c`
- Harness: `0.1.0+a13bb7b30a76`

| task | outcome | resolved | regressed | fail->pass | pass->pass | agent ms | validation ms | cost |
|---|---|---|---|---|---|---|---|---|
| pallets-flask-4045 | no_patch_produced | no | no | 0/1 | 1/1 | 210905 | 1458 | 0 |
| pallets-flask-4045 | no_patch_produced | no | no | 0/1 | 1/1 | 21470 | 995 | 0 |
| pallets-flask-4045 | no_patch_produced | no | no | 0/1 | 1/1 | 34779 | 1088 | 0 |
| pallets-flask-4992 | no_patch_produced | no | no | 0/1 | 1/1 | 393121 | 1911 | 0 |
| pallets-flask-4992 | no_patch_produced | no | no | 0/1 | 1/1 | 383380 | 897 | 0 |
| pallets-flask-4992 | no_patch_produced | no | no | 0/1 | 1/1 | 488348 | 968 | 0 |
| pallets-flask-5063 | failed (agent_error) | -- | -- | -- | -- | 937848 | -- | 0 |
| pallets-flask-5063 | failed (agent_error) | -- | -- | -- | -- | 930130 | -- | 0 |
| pallets-flask-5063 | failed (agent_error) | -- | -- | -- | -- | 933094 | -- | 0 |
| pytest-dev-pytest-11143 | no_patch_produced | no | no | 0/1 | 1/1 | 36564 | 3847 | 0 |
| pytest-dev-pytest-11143 | no_patch_produced | no | no | 0/1 | 1/1 | 58580 | 3358 | 0 |
| pytest-dev-pytest-11143 | no_patch_produced | no | no | 0/1 | 1/1 | 57791 | 3361 | 0 |
| pytest-dev-pytest-11148 | no_patch_produced | no | no | 0/1 | 1/1 | 30410 | 3675 | 0 |
| pytest-dev-pytest-11148 | no_patch_produced | no | no | 0/1 | 1/1 | 39985 | 3885 | 0 |
| pytest-dev-pytest-11148 | no_patch_produced | no | no | 0/1 | 1/1 | 33235 | 4596 | 0 |

5 of 5 task(s) agree across their repeats on result, patch and per-call tokens.

| task | runs | resolved | results | patches | token traces | agent ms, min/median/max | agrees |
|---|---|---|---|---|---|---|---|
| pallets-flask-4045 | 3 | 0/3 | no_patch_produced | 1 | 1 | 21470 / 34779 / 210905 | yes |
| pallets-flask-4992 | 3 | 0/3 | no_patch_produced | 1 | 1 | 383380 / 393121 / 488348 | yes |
| pallets-flask-5063 | 3 | 0/3 | agent_error | 0 | 1 | -- | yes |
| pytest-dev-pytest-11143 | 3 | 0/3 | no_patch_produced | 1 | 1 | 36564 / 57791 / 58580 | yes |
| pytest-dev-pytest-11148 | 3 | 0/3 | no_patch_produced | 1 | 1 | 30410 / 33235 / 39985 | yes |

### Produced no measurement (3)

| task | failure | from state | detail | run |
|---|---|---|---|---|
| pallets-flask-5063 | agent_error | agent_running | ollama did not answer within 900s | `20261115-095634-22448e78` |
| pallets-flask-5063 | agent_error | agent_running | ollama did not answer within 900s | `20261115-093032-b680b816` |
| pallets-flask-5063 | agent_error | agent_running | ollama did not answer within 900s | `20261115-090651-51be71a3` |
