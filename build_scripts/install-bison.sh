#!/bin/bash
# Top-level build script called from Dockerfile

# Stop at any error, show all commands
set -exuo pipefail

# Get script directory
MY_DIR=$(dirname "${BASH_SOURCE[0]}")

# Get build utilities
# shellcheck source-path=SCRIPTDIR
source "${MY_DIR}/build_utils.sh"

# Install bison. It isn't available via dnf on UBI8's AppStream/CRB repos (see
# the "Add bison if available" comment in install-runtime-packages.sh), but
# PostgreSQL's top-level ./configure hard-requires a real bison to generate
# src/backend/parser/gram.h -- a prerequisite pulled in by any `make` inside
# the source tree, including the libpq-only build in build-libpq.sh.
check_var "${BISON_ROOT}"
check_var "${BISON_HASH}"
check_var "${BISON_DOWNLOAD_URL}"

BISON_VERSION=${BISON_ROOT#*-}
if bison --version > /dev/null 2>&1; then
	# || test $? -eq 141 is there to ignore SIGPIPE with set -o pipefail
	# c.f. https://stackoverflow.com/questions/22464786/ignoring-bash-pipefail-for-error-code-141#comment60412687_33026977
	INSTALLED=$( (bison --version | head -1 || test $? -eq 141) | awk '{ print $NF }')
	SMALLEST=$(echo -e "${INSTALLED}\n${BISON_VERSION}" | sort -t. -k 1,1n -k 2,2n -k 3,3n -k 4,4n | head -1 || test $? -eq 141)
	if [ "${SMALLEST}" == "${BISON_VERSION}" ]; then
		echo "skipping installation of bison ${BISON_VERSION}, system provides bison ${INSTALLED}"
		exit 0
	fi
fi

fetch_source "${BISON_ROOT}.tar.gz" "${BISON_DOWNLOAD_URL}"
check_sha256sum "${BISON_ROOT}.tar.gz" "${BISON_HASH}"
tar -zxf "${BISON_ROOT}.tar.gz"
pushd "${BISON_ROOT}"
DESTDIR=/manylinux-rootfs do_standard_install
popd
rm -rf "${BISON_ROOT}" "${BISON_ROOT}.tar.gz"

# Strip what we can
strip_ /manylinux-rootfs

# Install
cp -rlf /manylinux-rootfs/* /

# Remove temporary rootfs
rm -rf /manylinux-rootfs

hash -r
bison --version
