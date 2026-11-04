#!/bin/bash
# Top-level build script called from Dockerfile

# Stop at any error, show all commands
set -exuo pipefail

# Get script directory
MY_DIR=$(dirname "${BASH_SOURCE[0]}")

# Get build utilities
# shellcheck source-path=SCRIPTDIR
source "${MY_DIR}/build_utils.sh"

# Build GPGME and its two GnuPG dependencies (libgpg-error, libassuan) from
# source. GPGME links both at runtime, so auditwheel bundles all three into the
# gpg wheel; building them here pins the versions we ship and lets us patch them
# for CVEs. GPGME 2.x only offers the Common Lisp ("cl") binding through
# configure -- the gpg PyPI package is a separate Python binding -- so
# --enable-languages= builds the C library alone.
check_var "${LIBGPG_ERROR_VERSION}"
check_var "${LIBGPG_ERROR_HASH}"
check_var "${LIBASSUAN_VERSION}"
check_var "${LIBASSUAN_HASH}"
check_var "${GPGME_VERSION}"
check_var "${GPGME_HASH}"
check_var "${GPGME_DOWNLOAD_URL}"

# All three libraries share one prefix so the single .so symlink pass in the
# Dockerfile picks up libgpg-error, libassuan and libgpgme together.
PREFIX=/opt/_internal/libgpgme-${GPGME_VERSION%%.*}

# Build one autotools library into PREFIX, installed live so the next link in the
# chain can find it, then drop the unpacked tree.
build_gnupg_lib() {
	local root=$1 subdir=$2 hash=$3
	shift 3
	fetch_source "${root}.tar.bz2" "${GPGME_DOWNLOAD_URL}/${subdir}"
	check_sha256sum "${root}.tar.bz2" "${hash}"
	tar xfj "${root}.tar.bz2"
	pushd "${root}"
	./configure --prefix="${PREFIX}" "$@" \
		CPPFLAGS="${MANYLINUX_CPPFLAGS} -I${PREFIX}/include" \
		CFLAGS="${MANYLINUX_CFLAGS}" \
		LDFLAGS="${MANYLINUX_LDFLAGS} -L${PREFIX}/lib -Wl,--enable-new-dtags,-rpath=${PREFIX}/lib" > /dev/null
	make -j"$(get_memory_safe_jobs 512)" > /dev/null
	make install > /dev/null
	popd
	rm -rf "${root}" "${root}.tar.bz2"
}

build_gnupg_lib "libgpg-error-${LIBGPG_ERROR_VERSION}" libgpg-error "${LIBGPG_ERROR_HASH}" \
	--disable-doc --enable-install-gpg-error-config
build_gnupg_lib "libassuan-${LIBASSUAN_VERSION}" libassuan "${LIBASSUAN_HASH}" \
	--with-libgpg-error-prefix="${PREFIX}"
build_gnupg_lib "gpgme-${GPGME_VERSION}" gpgme "${GPGME_HASH}" \
	--with-libgpg-error-prefix="${PREFIX}" --with-libassuan-prefix="${PREFIX}" \
	--enable-languages= --disable-gpg-test --disable-gpgconf-test

# Stage the installed prefix into the runtime rootfs.
mkdir -p "/manylinux-rootfs$(dirname "${PREFIX}")"
cp -a "${PREFIX}" "/manylinux-rootfs${PREFIX}"

# Strip what we can
strip_ /manylinux-rootfs

# Install for build (headers, pkgconfig and the *-config tools to compile wheels)
mkdir /manylinux-buildfs
cp -rlf /manylinux-rootfs/* /manylinux-buildfs/

mkdir -p /manylinux-buildfs/usr/local/lib/pkgconfig/
for pc in "${PREFIX}"/lib/pkgconfig/*.pc; do
	ln -s "${pc}" /manylinux-buildfs/usr/local/lib/pkgconfig/
done
mkdir -p /manylinux-buildfs/usr/local/bin/
for bin in "${PREFIX}"/bin/*; do
	ln -s "${bin}" /manylinux-buildfs/usr/local/bin/
done

# Clean-up for runtime: only the .so files need to ship
rm -rf /manylinux-rootfs"${PREFIX}"/include \
       /manylinux-rootfs"${PREFIX}"/lib/pkgconfig \
       /manylinux-rootfs"${PREFIX}"/bin \
       /manylinux-rootfs"${PREFIX}"/share
