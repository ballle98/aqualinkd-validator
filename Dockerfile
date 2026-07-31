FROM python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="AqualinkD Validator"
LABEL org.opencontainers.image.description="Validation and performance harness for AqualinkD"
LABEL org.opencontainers.image.source="https://github.com/ballle98/aqualinkd-validator"
LABEL org.opencontainers.image.revision="${VCS_REF}"

WORKDIR /opt/aqualinkd-validator

COPY src ./src

ENV PYTHONPATH=/opt/aqualinkd-validator/src

VOLUME ["/tmp/aqualinkd-validator-artifacts"]

ENTRYPOINT ["python", "-m", "aqualinkd_validator"]
CMD ["doctor"]
