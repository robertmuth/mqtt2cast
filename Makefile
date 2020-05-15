format:
	autopep8 -a -a -a -i *.py

lint:
	mypy mqtt2cast.py
