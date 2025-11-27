#!/bin/bash
#
# Install script for iNaturalist Invasives Monitor
# Run as root on Ubuntu 22.04/24.04
#

set -e

echo "=== iNaturalist Invasives Monitor Installation ==="

# Update system
apt-get update
apt-get upgrade -y

# Install system dependencies
apt-get install -y \
    python3.11 \
    python3.11-venv \
    python3-pip \
    redis-server \
    nginx \
    certbot \
    python3-certbot-nginx \
    git

# Create app user
if ! id -u invasives > /dev/null 2>&1; then
    useradd -m -s /bin/bash invasives
fi

# Create application directory
APP_DIR="/opt/invasives"
mkdir -p $APP_DIR
chown invasives:invasives $APP_DIR

echo "=== Cloning repository ==="
cd $APP_DIR
if [ ! -d "inat-diff" ]; then
    sudo -u invasives git clone https://github.com/YOUR_USERNAME/inat-diff.git
fi
cd inat-diff

echo "=== Setting up Python virtual environment ==="
sudo -u invasives python3.11 -m venv venv
sudo -u invasives ./venv/bin/pip install --upgrade pip

# Install the main package
sudo -u invasives ./venv/bin/pip install -e .

# Install webapp dependencies
sudo -u invasives ./venv/bin/pip install -r webapp/requirements.txt

echo "=== Creating directories ==="
mkdir -p /opt/invasives/inat-diff/webapp/reports
mkdir -p /var/log/invasives
chown -R invasives:invasives /opt/invasives
chown -R invasives:invasives /var/log/invasives

echo "=== Setting up environment file ==="
if [ ! -f /opt/invasives/.env ]; then
    cat > /opt/invasives/.env << 'EOF'
# iNaturalist Invasives Monitor Configuration
# Edit these values before starting the service

# Required: Mandrill API key for email
MANDRILL_API_KEY=your_mandrill_api_key_here

# Required: Change this to a random string
SECRET_KEY=change-this-to-random-string-$(openssl rand -hex 32)

# Domain configuration
BASE_URL=https://invasives.wildme.org
FROM_EMAIL=reports@invasives.wildme.org
FROM_NAME=iNaturalist Invasives Monitor

# Database (SQLite by default)
DATABASE_URL=sqlite:////opt/invasives/inat-diff/webapp/invasives.db

# Redis
REDIS_URL=redis://localhost:6379/0
EOF
    chown invasives:invasives /opt/invasives/.env
    chmod 600 /opt/invasives/.env
fi

echo "=== Installing systemd services ==="
# Copy service files
cp /opt/invasives/inat-diff/webapp/deploy/invasives-web.service /etc/systemd/system/
cp /opt/invasives/inat-diff/webapp/deploy/invasives-worker.service /etc/systemd/system/
cp /opt/invasives/inat-diff/webapp/deploy/invasives-beat.service /etc/systemd/system/

# Reload systemd
systemctl daemon-reload

# Enable services
systemctl enable invasives-web
systemctl enable invasives-worker
systemctl enable invasives-beat
systemctl enable redis-server

echo "=== Configuring Nginx ==="
cp /opt/invasives/inat-diff/webapp/deploy/nginx.conf /etc/nginx/sites-available/invasives
ln -sf /etc/nginx/sites-available/invasives /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Test nginx config
nginx -t

echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "1. Edit /opt/invasives/.env with your Mandrill API key"
echo "2. Create an admin user:"
echo "   cd /opt/invasives/inat-diff"
echo "   sudo -u invasives ./venv/bin/python -m webapp.manage create-admin admin yourpassword"
echo "3. Update nginx config with your domain"
echo "4. Run: certbot --nginx -d invasives.wildme.org"
echo "5. Start services:"
echo "   systemctl start redis-server"
echo "   systemctl start invasives-web"
echo "   systemctl start invasives-worker"
echo "   systemctl start invasives-beat"
echo "   systemctl restart nginx"
echo ""
echo "Monitor logs with:"
echo "   journalctl -u invasives-web -f"
echo "   journalctl -u invasives-worker -f"
