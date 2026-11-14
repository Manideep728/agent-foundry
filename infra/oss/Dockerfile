# The validation image for one real-repository task (D39): the task's own
# environment, with afrunner added as PID 1. Built by `af fetch`, which passes
# the instance image pinned by digest.
#
# afrunner is copied from afrunner:local rather than built again here. Its
# newest glibc symbol is GLIBC_2.34, which the Ubuntu 22.04 instance images
# provide, so one binary serves every task and a runner change is one rebuild.
ARG BASE_IMAGE
FROM afrunner:local AS runner

FROM ${BASE_IMAGE}
COPY --from=runner /usr/local/bin/afrunner /usr/local/bin/afrunner
ENTRYPOINT ["/usr/local/bin/afrunner"]
