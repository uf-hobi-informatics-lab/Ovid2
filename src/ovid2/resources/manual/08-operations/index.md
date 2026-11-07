---
title: Operations & security
summary: Run Ovid2 as a service for a team — deployment, configuration, SSO and roles, secrets, the image, logs and upgrades.
audience: [admin]
---

<div class="cards">
  <a class="card" href="deployment.md"><strong>Deployment</strong><span>systemd units, the reverse proxy, the host layout.</span></a>
  <a class="card" href="configuration.md"><strong>Configuration</strong><span>Every file and directory, and what lives where.</span></a>
  <a class="card" href="auth.md"><strong>Authentication & roles</strong><span>Google / Microsoft SSO, four roles, break-glass.</span></a>
  <a class="card" href="secrets.md"><strong>Secrets</strong><span>secrets.d, scoped executors, warn → enforce.</span></a>
  <a class="card" href="docker-image.md"><strong>The Docker image</strong><span>What it contains, and when to rebuild it.</span></a>
  <a class="card" href="logs-and-monitoring.md"><strong>Logs & monitoring</strong><span>Journals, event logs, archives, audit.</span></a>
  <a class="card" href="maintenance.md"><strong>Maintenance</strong><span>Backups, upgrades, retention, clean-up.</span></a>
</div>

## Production checklist

- [ ] The panel binds to **loopback** and a TLS reverse proxy fronts it.
- [ ] **Authentication enabled**, with at least two `system_admin`s and a
      break-glass password set.
- [ ] Every file in `secrets.d/` is mode **0600** and owned by the service
      user (`ovid2 doctor`).
- [ ] `OVID2_SECRETS_SCOPE_MODE=enforce` once the warn-mode logs are clean.
- [ ] Raw-feed storage accounts mounted **read-only** (`mount_mode: ro`).
- [ ] The image rebuilt from the deployed commit (`ovid2 image build`).
- [ ] The worker running as a service, with `--max-parallel` sized to the
      host.
- [ ] Backups of `$XDG_CONFIG_HOME/ovid2/` (including `secrets.d/` and the
      pipeline salts) and of `scheduler.db`.
- [ ] Retention set to match your policy (*Settings → Retention*).
