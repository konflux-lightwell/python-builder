#!/bin/bash
# Top-level build script called from Dockerfile

# Stop at any error, show all commands
set -exuo pipefail

# Get script directory
MY_DIR=$(dirname "${BASH_SOURCE[0]}")

# Get build utilities
# shellcheck source-path=SCRIPTDIR
source "${MY_DIR}/build_utils.sh"

# Build librdkafka (the Apache Kafka C/C++ client) from source so confluent-kafka
# compiles against a version we pin here and auditwheel bundles that exact
# librdkafka -- along with our source-built zlib, zstd and OpenSSL -- into the
# wheel, rather than whatever the distro ships.
check_var "${LIBRDKAFKA_VERSION}"
check_var "${LIBRDKAFKA_HASH}"
check_var "${LIBRDKAFKA_DOWNLOAD_URL}"
# Prefix of the source-built OpenSSL to link for SSL transport.
check_var "${OPENSSL_PREFIX}"

# librdkafka has no uploaded release tarball, so this is GitHub's auto-generated
# tag archive: named v<version>.tar.gz, it unpacks to librdkafka-<version>. The
# SHA-256 pin fails the build closed if GitHub ever regenerates those bytes.
LIBRDKAFKA_TARBALL="v${LIBRDKAFKA_VERSION}.tar.gz"
LIBRDKAFKA_ROOT="librdkafka-${LIBRDKAFKA_VERSION}"

# Install dir keyed by major version only, matching the libpq convention.
# (build-hdf5.sh deliberately uses major.minor instead -- HDF5's ABI is tracked
# per minor release.)
PREFIX=/opt/_internal/librdkafka-${LIBRDKAFKA_VERSION%%.*}

# librdkafka's configure detects zlib and zstd with a bare compile check (not
# pkg-config), so point the compiler and linker straight at our source-built
# prefixes. OpenSSL is found through pkg-config. Resolve each glob to its single
# prefix and fail closed if it is absent, rather than feeding configure a literal
# "zlib*"/"zstd*" path.
ZLIB_PREFIX=$(echo /opt/_internal/zlib*)
ZSTD_PREFIX=$(echo /opt/_internal/zstd*)
[ -d "${ZLIB_PREFIX}" ] || { echo "error: no zlib prefix under /opt/_internal" >&2; exit 1; }
[ -d "${ZSTD_PREFIX}" ] || { echo "error: no zstd prefix under /opt/_internal" >&2; exit 1; }
export PKG_CONFIG_PATH="${OPENSSL_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"

fetch_source "${LIBRDKAFKA_TARBALL}" "${LIBRDKAFKA_DOWNLOAD_URL}"
check_sha256sum "${LIBRDKAFKA_TARBALL}" "${LIBRDKAFKA_HASH}"
tar xfz "${LIBRDKAFKA_TARBALL}"
pushd "${LIBRDKAFKA_ROOT}"

export CPPFLAGS="${MANYLINUX_CPPFLAGS} -I${ZLIB_PREFIX}/include -I${ZSTD_PREFIX}/include -I${OPENSSL_PREFIX}/include"
export CFLAGS="${MANYLINUX_CFLAGS}"
export LDFLAGS="${MANYLINUX_LDFLAGS} -L${ZLIB_PREFIX}/lib -L${ZSTD_PREFIX}/lib -L${OPENSSL_PREFIX}/lib -Wl,--enable-new-dtags -Wl,-rpath=${PREFIX}/lib -Wl,-rpath=${ZLIB_PREFIX}/lib -Wl,-rpath=${ZSTD_PREFIX}/lib -Wl,-rpath=${OPENSSL_PREFIX}/lib"

# --enable-* (rather than the default "try") so a missing dependency fails the
# build loudly instead of silently dropping compression or TLS support. Cyrus
# SASL/GSSAPI and curl (OAUTHBEARER/OIDC) pull libraries we do not ship, so leave
# them off; the builtin PLAIN/SCRAM SASL mechanisms stay available through SSL,
# which is what confluent-kafka needs. Static linking is off by default (there is
# no --disable-static toggle), so it is not passed.
./configure --prefix="${PREFIX}" \
	--enable-ssl \
	--enable-zlib \
	--enable-zstd \
	--disable-gssapi \
	--disable-curl > /dev/null

make -j"$(get_memory_safe_jobs 512)" > /dev/null
make install DESTDIR=/manylinux-rootfs > /dev/null
popd
rm -rf "${LIBRDKAFKA_ROOT}" "${LIBRDKAFKA_TARBALL}"

# Strip what we can
strip_ /manylinux-rootfs

# Install for build (headers and pkgconfig are needed to compile wheels)
mkdir /manylinux-buildfs
cp -rlf /manylinux-rootfs/* /manylinux-buildfs/

# Expose rdkafka.pc (and rdkafka++.pc) on the standard build path. Guard the glob
# so an empty pkgconfig dir does not create a symlink named literally "*.pc".
mkdir -p /manylinux-buildfs/usr/local/lib/pkgconfig/
for pc in /manylinux-buildfs"${PREFIX}"/lib/pkgconfig/*.pc; do
	[ -e "${pc}" ] || continue
	ln -s "${PREFIX}/lib/pkgconfig/$(basename "${pc}")" /manylinux-buildfs/usr/local/lib/pkgconfig/
done

# Clean-up for runtime: only librdkafka.so* needs to ship
rm -rf /manylinux-rootfs"${PREFIX}"/include \
       /manylinux-rootfs"${PREFIX}"/lib/pkgconfig \
       /manylinux-rootfs"${PREFIX}"/bin \
       /manylinux-rootfs"${PREFIX}"/share
