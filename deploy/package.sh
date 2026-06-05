#!/bin/bash
set -e

echo "Bundling Core Job Tracker for Native OCI Build..."

# Navigate to project root
cd "$(dirname "$0")/.."

# Create a temporary staging directory
STAGING_DIR="release_staging"
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

# 1. Copy Application Source Code
echo "Copying source code..."
cp -r src "$STAGING_DIR/"
cp -r web "$STAGING_DIR/"
cp requirements.txt "$STAGING_DIR/"

# 2. Copy Docker Configuration
echo "Copying Docker configuration..."
mkdir -p "$STAGING_DIR/deploy"
cp deploy/Dockerfile "$STAGING_DIR/deploy/"
cp deploy/docker-compose.yml "$STAGING_DIR/deploy/"

# 3. Copy Required State Data
echo "Copying state data..."
cp config.yaml "$STAGING_DIR/"
cp -r targets "$STAGING_DIR/"
cp -r user_details "$STAGING_DIR/"

# 4. Generate Launch Script
echo "Generating launch script..."
cat << 'EOF' > "$STAGING_DIR/unpack_and_run.sh"
#!/bin/bash
echo "Building and Launching Core Job Tracker Natively on OCI..."
cd deploy
docker compose up --build -d
echo "Application running on port 8501. View logs with: cd deploy && docker compose logs -f"
EOF
chmod +x "$STAGING_DIR/unpack_and_run.sh"

# Zip everything up
echo "Creating release.zip..."
cd "$STAGING_DIR"
zip -r ../release.zip *
cd ..

# Cleanup
rm -rf "$STAGING_DIR"

echo "✅ release.zip successfully created in project root!"
echo "You can now SCP release.zip to your OCI host, unzip it, and run ./unpack_and_run.sh"
