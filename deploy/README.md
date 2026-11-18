# Deploy FrameFable on an existing VPS

These commands assume a Debian or Ubuntu-style Linux VPS with systemd and a reverse proxy that runs on the host. The VPS IP provided for this deployment is `72.60.165.248`. Replace `YOUR_SSH_USER` and every instance of `stories.example.com` with your SSH username and chosen full subdomain.

## 1. Check the current server

SSH to the VPS and identify the proxy already using ports 80 and 443. Also check whether FrameFable's default backend port is free.

```sh
ssh YOUR_SSH_USER@72.60.165.248
sudo ss -ltnp '( sport = :80 or sport = :443 or sport = :4173 )'
df -h /
free -h
```

If the public proxy is a container or is managed by a hosting panel, add the route through that proxy or panel. The templates here assume the proxy can reach the host's `127.0.0.1`. A container's own loopback is separate. Use the proxy's existing host-network integration or adapt the deployment to its private container network.

## 2. Point a new subdomain at the VPS

In Hostinger hPanel, open **Domains → your domain → DNS / Nameservers → DNS records**. Add an **A** record with **Name** `stories`, **Points to** `72.60.165.248`, and the default TTL. If you choose `framefable.example.com`, use `framefable` as the name instead.

Add or update only the record for the hostname you chose. If another provider controls the domain's authoritative nameservers, make the change there. The VPS already serves other subdomains, so reuse its current nameservers and its existing public web proxy. Check the record with `dig +short stories.example.com A`. [Hostinger subdomain DNS instructions](https://www.hostinger.com/support/8907694-how-to-create-a-subdomain-without-a-hosting-plan-at-hostinger/)

## 3. Upload and start FrameFable

From the project folder **on your Mac**, copy the application files. The virtual environment, downloaded weights, and credentials stay on your Mac.

```sh
rsync -av ./public ./deploy ./*.py ./requirements-*.txt ./run.sh YOUR_SSH_USER@72.60.165.248:framefable-upload/
```

On the VPS, install the core service. Run the `useradd` command only if the `framefable` account does not exist. If your SSH account is root, omit `sudo`.

```sh
sudo apt update
sudo apt install -y python3 python3-venv rsync
sudo useradd --system --user-group --home-dir /opt/framefable --shell /usr/sbin/nologin framefable
sudo install -d -o framefable -g framefable /opt/framefable
sudo rsync -a --chown=framefable:framefable "$HOME/framefable-upload/" /opt/framefable/
sudo -u framefable -H python3 -m venv /opt/framefable/.venv
sudo -u framefable -H /opt/framefable/.venv/bin/python -m pip install   -r /opt/framefable/requirements-pdf.txt -r /opt/framefable/requirements-audio.txt
sudo install -m 600 /opt/framefable/deploy/framefable.env.example /etc/framefable.env
sudoedit /etc/framefable.env
```

Set `FRAMEFABLE_PUBLIC_ORIGIN=https://stories.example.com` to the full hostname you chose. If port 4173 is busy, set `PORT` to a free port such as 4174 and use that same port in the reverse proxy. FrameFable stays bound to the VPS loopback interface.

```sh
sudo install -m 644 /opt/framefable/deploy/framefable.service /etc/systemd/system/framefable.service
sudo systemctl daemon-reload
sudo systemctl enable --now framefable
curl -fsS http://127.0.0.1:4173/ | grep -F '<title>FrameFable</title>'
```

Use your chosen backend port in the curl command. Check errors with `sudo journalctl -u framefable -n 80 --no-pager`.

## 4. Add one HTTPS route to your existing proxy

Add a named virtual host for **only** the FrameFable subdomain. Reuse ports 80 and 443 through the current proxy. Keep the Python port private.

- **Caddy 2.8 or later**. Run `sudo caddy hash-password` to get a password hash interactively. Add the block from `Caddyfile.example` to your current Caddy configuration, with your hostname, password hash, and backend port. Validate with `sudo caddy validate --config /etc/caddy/Caddyfile`, then run `sudo systemctl reload caddy`. Caddy issues HTTPS certificates when DNS points to the VPS and the existing proxy can answer ACME challenges. [Caddy HTTPS](https://caddyserver.com/docs/automatic-https), [Caddy password protection](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
- **Nginx**. Obtain a certificate for the new hostname through your existing certificate workflow. Install `apache2-utils` if you need `htpasswd`, then create a dedicated password file with `sudo htpasswd -cB /etc/nginx/framefable.htpasswd framefable`. Ensure the Nginx worker can read that file. Add the block from `nginx.conf.example` to the directory already included by your Nginx config. Set its hostname, certificate paths, and backend port. Check with `sudo nginx -t`, then run `sudo systemctl reload nginx`. Use your existing HTTP-to-HTTPS redirect pattern for this hostname. [Nginx reverse proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html), [Nginx password protection](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)

Verify that `curl -sS -o /dev/null -w '%{http_code}\n' https://stories.example.com/` returns `401` without credentials. Open the URL in a browser, sign in with the proxy credentials, and add your OpenAI key inside **Settings** to test story generation. Confirm that the other subdomains still load after the reload.

## 5. Enable features that run on the VPS

OpenAI story generation, bridge illustrations, and narration use the OpenAI API. If you select Ollama, the vision model must be installed on the VPS. By default, the hosted app connects to `http://127.0.0.1:11434` on the VPS.

If the VPS has spare RAM, disk, and CPU capacity, install Grounding DINO and MusicGen into its Python environment. MusicGen's checkpoint alone is about 2.36 GB. Benchmark both models on the VPS before relying on them alongside other services. While the weights are absent, the app uses the vision model's bounding boxes and shows a text story if background music is unavailable.

```sh
cd /opt/framefable
sudo -u framefable -H .venv/bin/python -m pip install -r requirements-detector.txt
sudo -u framefable -H .venv/bin/python detector.py
sudo -u framefable -H .venv/bin/python soundtrack.py
sudo systemctl restart framefable
```

## Automatic deployments

Once the service is working, follow the [GitHub Actions setup](CI-CD.md) to redeploy on each successful push to `main`.
