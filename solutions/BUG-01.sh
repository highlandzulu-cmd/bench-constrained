# Real fix: add() must add.
perl -pi -e 's/a - b/a + b/' src/lib.rs
