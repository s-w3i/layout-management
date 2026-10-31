# Setup and deployment

## Prerequisites

Install these on the machine that will run the application:

- Node.js 22 or later, with npm.
- CMake 3.16 or later.
- A C++20 compiler, such as GCC 11 or later.
- Git and internet access for the initial dependency installation.

On Ubuntu/Debian, install the build tools:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git
```

Install Node.js separately if necessary, then check its version:

```bash
node --version
npm --version
```

## Checkout and build

```bash
git clone --branch web-based --single-branch https://github.com/s-w3i/layout-management.git layout_generator
cd layout_generator
npm ci --include=dev
npm run build
```

Run these commands from the project directory.

## Start and stop

```bash
npm start
```

Open **http://127.0.0.1:4317**. Keep the terminal open; press **Ctrl+C** to stop the server.

Alternatively, the launcher installs missing dependencies, builds and starts the application:

```bash
./scripts/run.sh
```

To use another port:

```bash
PORT=4318 ./scripts/run.sh
```

Open **http://127.0.0.1:4318** when using that port. If a port is already occupied, stop the existing server in its terminal or choose a different port before starting another instance.

Use a browser with WebGL 2 support for the 3D view.

## Deploy on a remote Linux machine

Complete the prerequisite, checkout and build steps on the remote machine, then start the application there with `npm start`.

The server listens on `127.0.0.1`. From your own computer, open an SSH tunnel, replacing `user@server` with your SSH login:

```bash
ssh -N -L 4317:127.0.0.1:4317 user@server
```

Keep both the application and SSH tunnel running, then open **http://127.0.0.1:4317** on your computer. If using a custom port, use that port on both sides of the tunnel and in the browser URL.

## Back up and update

Stop the server with **Ctrl+C**. After the first run, back up the runtime data before updating:

```bash
tar -czf ../layout-generator-data-backup.tar.gz .data
```

Update, rebuild and restart:

```bash
git pull --ff-only origin web-based
npm ci --include=dev
npm run build
npm start
```

Keep `.data/` when updating or moving the deployment. To restore the backup into a stopped deployment, run this from the project directory:

```bash
tar -xzf ../layout-generator-data-backup.tar.gz
```
