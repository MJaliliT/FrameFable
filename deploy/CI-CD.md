# FrameFable CI/CD with GitHub Actions

A pull request into `main` runs tests. A push to `main` runs the same checks and sends the tested commit to the VPS over SSH if it is still the tip of `main`. The VPS stages the release, restarts only `framefable.service`, then checks the site on loopback. If the release fails, it switches back to the previous one. The first deployment assumes that FrameFable already works on the VPS using the [manual deployment guide](README.md).

## 1. Prepare the VPS once

Run these commands on the VPS as your regular administrator. Run `useradd` just once.

```sh
sudo useradd --create-home --user-group --shell /bin/bash framefable-deploy
sudo install -d -o framefable-deploy -g framefable-deploy -m 755 /opt/framefable/releases
sudo -u framefable-deploy ln -s /opt/framefable /opt/framefable/releases/current

sudo install -m 755 /opt/framefable/deploy/activate-release.sh /usr/local/bin/framefable-deploy
sudo install -D -m 644 /opt/framefable/deploy/cicd.override.conf   /etc/systemd/system/framefable.service.d/cicd.conf
sudo visudo -f /etc/sudoers.d/framefable-deploy
```

Add this single rule and save:

```text
framefable-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart framefable.service
```

If you set a backend port other than 4173 in `/etc/framefable.env`, edit the `port=` line in `/usr/local/bin/framefable-deploy` to match. Apply the service override and check that the existing release still works. Use your actual port in the curl command.

```sh
sudo systemctl daemon-reload
sudo systemctl restart framefable
curl -fsS http://127.0.0.1:4173/ | grep -F '<title>FrameFable</title>'
```

The release has its own application directory. It reuses the server's existing `.venv`, model weights, and `/etc/framefable.env`. If you change a Python dependency, install or upgrade it in the shared VPS environment before deploying the code that needs it. Changes to proxy, DNS, and systemd templates are applied separately by the VPS operator.

## 2. Set up GitHub’s SSH access

On your Mac, create a dedicated key for GitHub Actions.

```sh
ssh-keygen -t ed25519 -N '' -C framefable-github-actions -f ~/.ssh/framefable_actions
```

On the VPS, install the public key from `~/.ssh/framefable_actions.pub` on your Mac in `/home/framefable-deploy/.ssh/authorized_keys`. Use the single line `restrict ssh-ed25519 …`, replacing the ellipsis with the rest of your actual public key. The directory and file should be owned by `framefable-deploy`, with modes `700` and `600` respectively. The `restrict` option disables features such as port forwarding for this key.

Get the server's SSH host key using your already trusted administrative session, then compare its fingerprint with the key returned by `ssh-keyscan` on your Mac. For the default SSH port and the VPS IP in this project:

```sh
# On the VPS, through your trusted administrative session
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub

# On your Mac
ssh-keyscan -p 22 -t ed25519 72.60.165.248 > /tmp/framefable-known-hosts
ssh-keygen -lf /tmp/framefable-known-hosts
```

Create an empty private GitHub repository named `framefable` or use your existing project repository. In **Settings → Secrets and variables → Actions**, set these **repository secrets**. [GitHub Actions secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

| Secret | Value |
| --- | --- |
| `VPS_SSH_KEY` | The private key in `~/.ssh/framefable_actions` on your Mac |
| `VPS_KNOWN_HOSTS` | The full contents of the verified `/tmp/framefable-known-hosts` file |

The workflow defaults to `72.60.165.248` and port `22`. If either differs, set `VPS_HOST` and/or `VPS_PORT` as repository **variables**. Use the same host and port when creating the known-hosts entry. Give write access only to people you trust to deploy FrameFable.

## 3. Push the project

If this folder is new to Git and the GitHub repository is empty, start from the project folder on your Mac:

```sh
git init -b main
git add .
git status --short
git commit -m "Add FrameFable and automatic VPS deployment"
git remote add origin git@github.com:YOUR_GITHUB_USERNAME/framefable.git
git push -u origin main
```

Review the staged list before committing. The project ignores local model weights, the virtual environment, and common credential filenames. If the repository already has commits, add the project to its existing checkout and push normally. [Adding local code to GitHub](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github?platform=mac)

On GitHub, open **Actions → CI and VPS deployment** to see the build and deployment. After the first successful run, open your FrameFable HTTPS subdomain. Future commits deployed from `main` use the same subdomain.
