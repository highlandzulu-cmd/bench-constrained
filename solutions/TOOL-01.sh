# Real fix: add() must add, and is_even must not trigger clippy's needless_return / needless_bool.
perl -pi -e 's/a - b/a + b/' src/lib.rs
perl -0pi -e 's/if n % 2 == 0 \{\s*return true;\s*\} else \{\s*return false;\s*\}/n % 2 == 0/s' src/lib.rs
