#!/bin/bash
# Top-level build script called from Dockerfile

# Stop at any error, show all commands
set -exuo pipefail

# Get script directory
MY_DIR=$(dirname "${BASH_SOURCE[0]}")

# Get build utilities
# shellcheck source-path=SCRIPTDIR
source "${MY_DIR}/build_utils.sh"

# Install a more recent Rust
check_var "${RUST_VERSION}"
check_var "${RUSTUP_DOWNLOAD_URL}"

PREFIX=/opt/_internal/rust-${RUST_VERSION}

# Download and verify rustup-init. Hashes are for rustup-init from
# https://static.rust-lang.org/rustup/archive/<RUSTUP_VERSION>/<triple>/rustup-init
# (pinned via RUSTUP_DOWNLOAD_URL in the Containerfile; currently 1.29.1).
RUSTUP_INIT="rustup-init"
case "${AUDITWHEEL_ARCH}" in
    x86_64)
        RUSTUP_ARCH="x86_64-unknown-linux-gnu"
        RUSTUP_HASH="dda7234360b7f578ca8b0ddcb80145646fa61a67c1720a5abc7051b35c9fcb71"
        ;;
    aarch64)
        RUSTUP_ARCH="aarch64-unknown-linux-gnu"
        RUSTUP_HASH="15f6e4ce9f583b929c996c91562bad6d4454f3281de858b02cdfdef615fac433"
        ;;
    s390x)
        RUSTUP_ARCH="s390x-unknown-linux-gnu"
        RUSTUP_HASH="312ac9390467fd7870f8aff6557c7cb3bff0a04bb11b263173fdfd81586da641"
        ;;
    ppc64le)
        RUSTUP_ARCH="powerpc64le-unknown-linux-gnu"
        RUSTUP_HASH="86ebc5b46b20013fca57dff4f83aa0c325ee90914626ce1c80ae76e126e48afb"
        ;;
    i686)
        RUSTUP_ARCH="i686-unknown-linux-gnu"
        RUSTUP_HASH="5b993042a9f0d3577592f08cb76f8b850951487c2556d90f6e9846abbb083a88"
        ;;
    armv7l)
        RUSTUP_ARCH="armv7-unknown-linux-gnueabihf"
        RUSTUP_HASH="6f34abb0d553273ce08306ea3adb758d0171f21090cc5ad5426f474ded5504d1"
        ;;
    *)
        echo "Unsupported architecture: ${AUDITWHEEL_ARCH}"
        exit 1
        ;;
esac

fetch_source "${RUSTUP_INIT}" "${RUSTUP_DOWNLOAD_URL}/${RUSTUP_ARCH}"
check_sha256sum "${RUSTUP_INIT}" "${RUSTUP_HASH}"
chmod +x "${RUSTUP_INIT}"

# Install Rust using rustup to PREFIX
RUSTUP_HOME="${PREFIX}/rustup"
CARGO_HOME="${PREFIX}/cargo"
export RUSTUP_HOME CARGO_HOME

./"${RUSTUP_INIT}" -y --default-toolchain "${RUST_VERSION}" --profile minimal --no-modify-path

# Create symlinks for runtime
mkdir -p /manylinux-rootfs"${PREFIX}"
cp -r "${CARGO_HOME}" /manylinux-rootfs"${PREFIX}"/
cp -r "${RUSTUP_HOME}" /manylinux-rootfs"${PREFIX}"/

# OCP runs containers with an arbitrary UID but GID 0. Make only the runtime
# cache directories group-writable so non-root users can create the registry
# cache at runtime without making executables writable.
mkdir -p /manylinux-rootfs"${PREFIX}"/cargo/{registry,git}
chmod -R g+w /manylinux-rootfs"${PREFIX}"/cargo/{registry,git}/

# A crate can pin a different toolchain via rust-toolchain.toml, which makes
# rustup install it at build time. That writes to tmp, downloads, toolchains and
# update-hashes, and updates settings.toml. Not recursive: toolchains/ only needs
# to be writable so a new toolchain directory can be created, and -R here would
# make already-installed toolchain binaries writable too.
mkdir -p /manylinux-rootfs"${PREFIX}"/rustup/{tmp,downloads,toolchains,update-hashes}
chmod g+w /manylinux-rootfs"${PREFIX}"/rustup \
          /manylinux-rootfs"${PREFIX}"/rustup/{tmp,downloads,toolchains,update-hashes}
chmod g+w /manylinux-rootfs"${PREFIX}"/rustup/settings.toml

# Create wrapper scripts that set environment variables
mkdir -p /manylinux-rootfs/usr/local/bin
cat > /manylinux-rootfs/usr/local/bin/rustc <<EOF
#!/bin/bash
export RUSTUP_HOME="${PREFIX}/rustup"
export CARGO_HOME="${PREFIX}/cargo"
exec "${PREFIX}/cargo/bin/rustc" "\$@"
EOF
chmod +x /manylinux-rootfs/usr/local/bin/rustc

cat > /manylinux-rootfs/usr/local/bin/cargo <<EOF
#!/bin/bash
export RUSTUP_HOME="${PREFIX}/rustup"
export CARGO_HOME="${PREFIX}/cargo"
exec "${PREFIX}/cargo/bin/cargo" "\$@"
EOF
chmod +x /manylinux-rootfs/usr/local/bin/cargo

cat > /manylinux-rootfs/usr/local/bin/rustup <<EOF
#!/bin/bash
export RUSTUP_HOME="${PREFIX}/rustup"
export CARGO_HOME="${PREFIX}/cargo"
exec "${PREFIX}/cargo/bin/rustup" "\$@"
EOF
chmod +x /manylinux-rootfs/usr/local/bin/rustup

# Clean up
rm -f "${RUSTUP_INIT}"

# Strip what we can (be careful with Rust binaries, some stripping can break them)
# strip_ /manylinux-rootfs  # Commented out to avoid breaking Rust binaries

# Install for build
mkdir /manylinux-buildfs
cp -rlf /manylinux-rootfs/* /manylinux-buildfs/

# Verify installation
export RUSTUP_HOME="${PREFIX}/rustup"
export CARGO_HOME="${PREFIX}/cargo"
"${PREFIX}/cargo/bin/rustc" --version
"${PREFIX}/cargo/bin/cargo" --version
