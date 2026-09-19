# Overwritten by the CI at build time (see .github/workflows/ci.yml):
#   release build -> "4"     (v1, v2, v3, ... picked automatically by release.yml)
#   other pushes  -> "4.3"   (last release number + commits since it)
# The value below is only used when running from source.
__version__ = "1.0.1"
