# Real fix: clearCompleted drops completed todos.
perl -0pi -e 's/(function clearCompleted\(todos\) \{.*?)return todos\.slice\(\);/$1return todos.filter((todo) => !todo.completed);/s' js/todo.js
