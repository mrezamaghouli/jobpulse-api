# Shared Tor daemon image for both the local dev/testing overlay
# (docker-compose.tor.yml, which builds this Dockerfile directly) and
# production (built and published as an immutable
# ghcr.io/mrezamaghouli/jobpulse-tor:<tag> image by
# .github/workflows/tor-image-build.yml, consumed by
# docker-compose.prod.tor.yml -- production never uses `build:`). Not
# built or referenced by the default docker-compose.yml or by
# docker-compose.prod.yml itself.
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tor netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY torrc.base /etc/tor/torrc.base
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 9050 9051

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
