#!/bin/bash
# Top-level build script called from Dockerfile

# Stop at any error, show all commands
set -exuo pipefail

# Get script directory
MY_DIR=$(dirname "${BASH_SOURCE[0]}")

# Get build utilities
# shellcheck source-path=SCRIPTDIR
source "${MY_DIR}/build_utils.sh"

# Build libpq (the PostgreSQL C client library) from the PostgreSQL source tree.
# Only the client pieces are built -- the shared library, its headers and
# pg_config -- so psycopg2 compiles against a version we pin here and auditwheel
# bundles that exact libpq into the wheel, rather than whatever the distro ships.
check_var "${LIBPQ_VERSION}"
check_var "${LIBPQ_HASH}"
check_var "${LIBPQ_DOWNLOAD_URL}"
# Optional: prefix of the source-built OpenSSL to link for TLS connections.
: "${OPENSSL_PREFIX:=}"
LIBPQ_ROOT="postgresql-${LIBPQ_VERSION}"

PREFIX=/opt/_internal/libpq-${LIBPQ_VERSION%%.*}

fetch_source "${LIBPQ_ROOT}.tar.bz2" "${LIBPQ_DOWNLOAD_URL}"
check_sha256sum "${LIBPQ_ROOT}.tar.bz2" "${LIBPQ_HASH}"
tar xfj "${LIBPQ_ROOT}.tar.bz2"
pushd "${LIBPQ_ROOT}"

CONFIGURE_OPTS=(--prefix="${PREFIX}" --without-readline --without-zlib --without-icu)
LIBPQ_CPPFLAGS="${MANYLINUX_CPPFLAGS}"
LIBPQ_LDFLAGS="${MANYLINUX_LDFLAGS} -Wl,--enable-new-dtags,-rpath=${PREFIX}/lib"
if [ -n "${OPENSSL_PREFIX}" ]; then
	CONFIGURE_OPTS+=(--with-openssl)
	LIBPQ_CPPFLAGS="${LIBPQ_CPPFLAGS} -I${OPENSSL_PREFIX}/include"
	LIBPQ_LDFLAGS="${LIBPQ_LDFLAGS} -L${OPENSSL_PREFIX}/lib -Wl,-rpath=${OPENSSL_PREFIX}/lib"
else
	CONFIGURE_OPTS+=(--without-openssl)
fi

# PostgreSQL's top-level ./configure hard-requires both bison and flex,
# regardless of what's actually built afterwards. bison is genuine -- building
# src/interfaces/libpq pulls in submake-generated-headers, which needs a real
# bison to produce src/backend/parser/gram.h (see install-bison.sh) -- but
# flex is only ever checked here, never invoked: generated-headers doesn't
# depend on any flex-generated file (scan.c is a backend-only, non-libpq
# build target), so a stub satisfies the check without needing a real flex.
./configure "${CONFIGURE_OPTS[@]}" FLEX=true \
	CPPFLAGS="${LIBPQ_CPPFLAGS}" CFLAGS="${MANYLINUX_CFLAGS}" LDFLAGS="${LIBPQ_LDFLAGS}" > /dev/null

# Build only the client library and pg_config; libpq's Makefile pulls in the
# libpgport/libpgcommon submakes it needs, so the server is never built.
make -C src/interfaces/libpq -j"$(get_memory_safe_jobs 512)" > /dev/null
make -C src/bin/pg_config -j"$(get_memory_safe_jobs 512)" > /dev/null

DESTDIR=/manylinux-rootfs make -C src/interfaces/libpq install > /dev/null
DESTDIR=/manylinux-rootfs make -C src/bin/pg_config install > /dev/null
# Client-facing headers (postgres_ext.h, pg_config_ext.h, ...).
DESTDIR=/manylinux-rootfs make -C src/include install > /dev/null
popd
rm -rf "${LIBPQ_ROOT}" "${LIBPQ_ROOT}.tar.bz2"

# Strip what we can
strip_ /manylinux-rootfs

# Install for build (headers, pkgconfig and pg_config are needed to compile wheels)
mkdir /manylinux-buildfs
cp -rlf /manylinux-rootfs/* /manylinux-buildfs/

# Expose libpq.pc and pg_config on the standard build paths
mkdir -p /manylinux-buildfs/usr/local/lib/pkgconfig/
ln -s "${PREFIX}/lib/pkgconfig/libpq.pc" /manylinux-buildfs/usr/local/lib/pkgconfig/libpq.pc
mkdir -p /manylinux-buildfs/usr/local/bin/
ln -s "${PREFIX}/bin/pg_config" /manylinux-buildfs/usr/local/bin/pg_config

# Clean-up for runtime: only libpq.so* needs to ship
rm -rf /manylinux-rootfs"${PREFIX}"/include \
       /manylinux-rootfs"${PREFIX}"/lib/pkgconfig \
       /manylinux-rootfs"${PREFIX}"/bin \
       /manylinux-rootfs"${PREFIX}"/share
