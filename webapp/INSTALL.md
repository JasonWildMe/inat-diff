# Installing iNaturalist Invasives Monitor on Azure Ubuntu 24.04

This guide walks through deploying the invasive species monitoring web application on a fresh Azure Ubuntu 24.04 server.

## Prerequisites

Before starting, you'll need:

1. **Azure account** with ability to create VMs
2. **Domain name** pointed to your server (e.g., `invasives.wildme.org`)
3. **Mandrill account** for sending emails (free tier: 500 emails/month)
4. **SSH client** to connect to your server

---

## Step 1: Create Azure VM

### In Azure Portal:

1. Go to **Virtual Machines** → **Create**
2. Configure:
   - **Image**: Ubuntu Server 24.04 LTS
   - **Size**: Standard_B2s (2 vCPU, 4GB RAM) or larger
   - **Authentication**: SSH public key (recommended)
   - **Username**: `azureuser` (or your preference)

3. **Networking** → Add inbound rules:
   - Port 22 (SSH)
   - Port 80 (HTTP)
   - Port 443 (HTTPS)

4. **Review + Create** → **Create**

5. Note your server's **public IP address**

---

## Step 2: Configure DNS

In your DNS provider, create an A record:

```
invasives.wildme.org  →  YOUR_SERVER_IP
```

Allow 5-30 minutes for DNS propagation. Verify with:

```bash
nslookup invasives.wildme.org
```

---

## Step 3: Get Mandrill API Key

1. Sign up at [mailchimp.com](https://mailchimp.com)
2. Go to **Transactional Email** (Mandrill)
3. **Settings** → **SMTP & API Info** → **Create API Key**
4. Save the API key for later

---

## Step 4: Connect to Server

```bash
ssh azureuser@YOUR_SERVER_IP
```

Or if using a key file:

```bash
ssh -i ~/.ssh/your-key.pem azureuser@YOUR_SERVER_IP
```

---

## Step 5: Run Installation Script

```bash
# Become root
sudo -i

# Download the install script
# Option A: If repo is public on GitHub
curl -O https://raw.githubusercontent.com/YOUR_USERNAME/inat-diff/main/webapp/deploy/install.sh

# Option B: Clone the repo first
git clone https://github.com/YOUR_USERNAME/inat-diff.git
cp inat-diff/webapp/deploy/install.sh .

# Make executable and run
chmod +x install.sh
./install.sh
```

The script will:
- Update system packages
- Install Python, Redis, Nginx, Certbot
- Create `invasives` user
- Clone the repository
- Set up Python virtual environment
- Install dependencies
- Create systemd service files
- Configure Nginx

---

## Step 6: Configure Environment

The install script auto-generates a secure `SECRET_KEY`. You only need to add your Mandrill API key.

Edit the environment file:

```bash
nano /opt/invasives/.env
```

Update the Mandrill API key:

```bash
# Your Mandrill API key from Step 3
MANDRILL_API_KEY=your_actual_api_key_here
```

Optionally update if using a different domain:

```bash
BASE_URL=https://invasives.wildme.org
FROM_EMAIL=reports@invasives.wildme.org
```

Save and exit (Ctrl+X, Y, Enter).

---

## Step 7: Create Admin User

```bash
cd /opt/invasives/inat-diff
sudo -u invasives ./venv/bin/python -m webapp.manage create-admin admin YourSecurePassword123
```

**Important**: Use a strong password! This protects your admin dashboard.

---

## Step 8: Set Up SSL Certificate

```bash
# Get SSL certificate from Let's Encrypt
certbot --nginx -d invasives.wildme.org
```

Follow the prompts:
- Enter email for renewal notices
- Agree to terms
- Choose whether to redirect HTTP to HTTPS (recommended: Yes)

---

## Step 9: Start Services

```bash
# Start all services
systemctl start redis-server
systemctl start invasives-web
systemctl start invasives-worker
systemctl start invasives-beat
systemctl restart nginx

# Verify they're running
systemctl status invasives-web
systemctl status invasives-worker
systemctl status invasives-beat
```

---

## Step 10: Verify Installation

### Check health endpoint:

```bash
curl http://localhost:8000/health
```

Should return: `{"status":"healthy","timestamp":"..."}`

### Check the website:

Open in browser: `https://invasives.wildme.org`

You should see the subscription form.

### Check admin dashboard:

Open: `https://invasives.wildme.org/admin`

Enter the admin credentials you created in Step 7.

---

## Step 11: Test Email (Optional)

1. Subscribe with your email on the main page
2. Check logs for verification email:
   ```bash
   journalctl -u invasives-worker -f
   ```
3. Check your inbox for the verification email

---

## Maintenance Commands

### View logs:

```bash
# Web application
journalctl -u invasives-web -f

# Background worker (report generation)
journalctl -u invasives-worker -f

# Scheduler
journalctl -u invasives-beat -f
```

### Restart services:

```bash
systemctl restart invasives-web
systemctl restart invasives-worker
systemctl restart invasives-beat
```

### Update the application:

```bash
cd /opt/invasives/inat-diff
sudo -u invasives git pull
sudo -u invasives ./venv/bin/pip install -e .
sudo -u invasives ./venv/bin/pip install -r webapp/requirements.txt
systemctl restart invasives-web invasives-worker invasives-beat
```

### Backup database:

```bash
cp /opt/invasives/inat-diff/webapp/invasives.db /opt/invasives/backups/invasives-$(date +%Y%m%d).db
```

### Renew SSL certificate:

Certbot auto-renews, but you can manually renew:

```bash
certbot renew
```

---

## Troubleshooting

### "502 Bad Gateway" in browser

The web service isn't running:

```bash
systemctl status invasives-web
journalctl -u invasives-web -n 50
```

### Emails not sending

1. Check Mandrill API key is correct in `/opt/invasives/.env`
2. Verify FROM_EMAIL domain in Mandrill dashboard
3. Check worker logs: `journalctl -u invasives-worker -f`

### Reports not generating

1. Check worker is running: `systemctl status invasives-worker`
2. Check Redis: `systemctl status redis-server`
3. View worker logs: `journalctl -u invasives-worker -f`

### Can't access admin dashboard

1. Verify admin user exists:
   ```bash
   cd /opt/invasives/inat-diff
   sudo -u invasives ./venv/bin/python -m webapp.manage list-admins
   ```

2. Reset password if needed:
   ```bash
   sudo -u invasives ./venv/bin/python -m webapp.manage reset-password admin newpassword
   ```

### Permission errors

```bash
chown -R invasives:invasives /opt/invasives
```

---

## Architecture Overview

```
Internet
    │
    ▼
┌─────────────────┐
│     Nginx       │ ← SSL termination, reverse proxy
│   (port 443)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    FastAPI      │ ← Web application
│   (port 8000)   │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌───────┐ ┌───────────┐
│ Redis │ │  Celery   │ ← Background tasks
│       │ │  Worker   │
└───────┘ └─────┬─────┘
                │
         ┌──────┴──────┐
         │             │
         ▼             ▼
   ┌──────────┐  ┌──────────┐
   │iNaturalist│  │ Mandrill │
   │   API    │  │  (Email) │
   └──────────┘  └──────────┘
```

---

## Service Files Location

- **Web service**: `/etc/systemd/system/invasives-web.service`
- **Worker service**: `/etc/systemd/system/invasives-worker.service`
- **Scheduler service**: `/etc/systemd/system/invasives-beat.service`
- **Nginx config**: `/etc/nginx/sites-available/invasives`
- **Environment file**: `/opt/invasives/.env`
- **Application code**: `/opt/invasives/inat-diff`
- **Database**: `/opt/invasives/inat-diff/webapp/invasives.db`
- **Generated reports**: `/opt/invasives/inat-diff/webapp/reports/`

---

## Support

- **GitHub Issues**: https://github.com/YOUR_USERNAME/inat-diff/issues
- **iNaturalist**: https://www.inaturalist.org
