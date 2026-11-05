# Real fix: remainingCount counts only incomplete todos.
perl -0pi -e 's/return todos\.length;/return todos.filter((todo) => !todo.completed).length;/' js/todo.js
