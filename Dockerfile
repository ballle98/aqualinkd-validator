FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="AqualinkD Validator"
LABEL org.opencontainers.image.description="Validation and performance harness for AqualinkD"
LABEL org.opencontainers.image.source="https://github.com/ballle98/aqualinkd-validator"
LABEL org.opencontainers.image.revision="${VCS_REF}"

WORKDIR /opt/aqualinkd-validator

COPY src ./src

ENV PYTHONPATH=/opt/aqualinkd-validator/src

VOLUME ["/artifacts"]

ENTRYPOINT ["python", "-m", "aqualinkd_validator"]
CMD ["doctor"]
