# Real fix: accept --verbose as well as -v.
perl -pi -e 's/a == "-v"/a == "-v" || a == "--verbose"/' src/lib.rs
