#!/bin/bash
# Top-level build script called from Dockerfile

# Stop at any error, show all commands
set -exuo pipefail

# Get script directory
MY_DIR=$(dirname "${BASH_SOURCE[0]}")

# Get build utilities
# shellcheck source-path=SCRIPTDIR
source "${MY_DIR}/build_utils.sh"

# Build the HDF5 C library from source so h5py compiles against a version we pin
# here and auditwheel bundles that exact libhdf5 -- plus our source-built zlib for
# the deflate filter -- into the wheel. Only the C library and its high-level API
# are built; Fortran, C++, Java, the command-line tools and the test suite are off.
check_var "${HDF5_VERSION}"
check_var "${HDF5_HASH}"
check_var "${HDF5_DOWNLOAD_URL}"

HDF5_ROOT="hdf5-${HDF5_VERSION}"

# Install dir keyed by major.minor -- unlike the major-only scheme libpq and
# librdkafka use -- because HDF5's ABI/soname is tracked per minor release. The
# stable unversioned symlink below lets the image's HDF5_DIR stay fixed across bumps.
PREFIX=/opt/_internal/hdf5-${HDF5_VERSION%.*}
STABLE=/opt/_internal/hdf5

# Resolve the zlib glob to its single prefix and fail closed if it is absent,
# rather than feeding configure a literal "zlib*" path.
ZLIB_PREFIX=$(echo /opt/_internal/zlib*)
[ -d "${ZLIB_PREFIX}" ] || { echo "error: no zlib prefix under /opt/_internal" >&2; exit 1; }

fetch_source "${HDF5_ROOT}.tar.gz" "${HDF5_DOWNLOAD_URL}"
check_sha256sum "${HDF5_ROOT}.tar.gz" "${HDF5_HASH}"
tar xfz "${HDF5_ROOT}.tar.gz"
pushd "${HDF5_ROOT}"

# --with-zlib points at the prefix; the explicit -I/-L below make the compile and
# link checks succeed regardless of how configure interprets that directory.
./configure --prefix="${PREFIX}" \
	--disable-static \
	--enable-shared \
	--enable-hl \
	--disable-fortran \
	--disable-cxx \
	--disable-java \
	--disable-tools \
	--disable-tests \
	--with-zlib="${ZLIB_PREFIX}" \
	--without-szlib \
	CPPFLAGS="${MANYLINUX_CPPFLAGS} -I${ZLIB_PREFIX}/include" \
	CFLAGS="${MANYLINUX_CFLAGS}" \
	LDFLAGS="${MANYLINUX_LDFLAGS} -L${ZLIB_PREFIX}/lib -Wl,--enable-new-dtags,-rpath=${PREFIX}/lib -Wl,-rpath=${ZLIB_PREFIX}/lib" > /dev/null

make -j"$(get_memory_safe_jobs 512)" > /dev/null
make install DESTDIR=/manylinux-rootfs > /dev/null
popd
rm -rf "${HDF5_ROOT}" "${HDF5_ROOT}.tar.gz"

# Strip what we can
strip_ /manylinux-rootfs

# Install for build (headers and pkgconfig are needed to compile wheels)
mkdir /manylinux-buildfs
cp -rlf /manylinux-rootfs/* /manylinux-buildfs/

# h5py locates HDF5 through $HDF5_DIR; expose a stable symlink to the versioned
# prefix so the image can set HDF5_DIR once.
ln -s "hdf5-${HDF5_VERSION%.*}" "/manylinux-buildfs${STABLE}"

# Clean-up for runtime: only the .so files need to ship
rm -rf /manylinux-rootfs"${PREFIX}"/include \
       /manylinux-rootfs"${PREFIX}"/lib/pkgconfig \
       /manylinux-rootfs"${PREFIX}"/lib/libhdf5.settings \
       /manylinux-rootfs"${PREFIX}"/bin \
       /manylinux-rootfs"${PREFIX}"/share
