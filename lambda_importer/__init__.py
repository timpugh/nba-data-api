# Marks lambda_importer/ as a Python package so mypy can distinguish
# lambda_importer.app from lambda/app.py without a "duplicate module"
# error. PythonFunction bundles every file in the entry directory, so this
# empty file just rides along in the Lambda zip — no runtime impact.
