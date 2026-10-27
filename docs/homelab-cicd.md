# Homelab CI/CD contract
Merge into main builds the immutable Docker image. Deployment remains disabled until the homelab runner and production Compose service are configured.
1. Register a **dedicated trusted self-hosted GitHub Actions runner** labelled `homelab-deploy`; use a dedicated OS user and isolate the runner from untrusted workflows. Never run pull-request jobs on that runner.
2. Set repository variables: `HOMELAB_DEPLOY_ENABLED=true`, `HOMELAB_COMPOSE_DIR=/absolute/path/to/compose/directory`, `HOMELAB_COMPOSE_SERVICE=service-name`.
3. Service must use `image: ${HOMELAB_IMAGE}` and define a functional Docker HEALTHCHECK. This image variable is injected only for the selected service; avoid sharing it among multiple services.
4. Ensure registry pull authorization, file permissions, protected main and branch review rules. Protect production from unreviewed workflows.
5. Before first activation, test rollback and persistence on a staging replica. This rollback restores the previous container image, **not database migrations**.
6. The runner's Docker access effectively grants host-level privileges; isolate it in a dedicated VM, not the Proxmox host, and only register trusted repositories.
Do not delete volumes or run `docker compose down -v`. Application secrets belong on the homelab, never in Git.
