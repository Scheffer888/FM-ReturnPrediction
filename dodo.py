"""Run or update the project. This file uses the `doit` Python package. It works
like a Makefile, but is Python-based

"""

import sys

sys.path.insert(1, "./src/")

import shutil
import shlex
from os import environ, getcwd, path
from pathlib import Path

from settings import config
from colorama import Fore, Style, init

# ====================================================================================
# PyDoit Formatting
# ====================================================================================

## Custom reporter: Print PyDoit Text in Green
# This is helpful because some tasks write to sterr and pollute the output in
# the console. I don't want to mute this output, because this can sometimes
# cause issues when, for example, LaTeX hangs on an error and requires
# presses on the keyboard before continuing. However, I want to be able
# to easily see the task lines printed by PyDoit. I want them to stand out
# from among all the other lines printed to the console.
from doit.reporter import ConsoleReporter

try:
    in_slurm = environ["SLURM_JOB_ID"] is not None
except:
    in_slurm = False


class GreenReporter(ConsoleReporter):
    def write(self, stuff, **kwargs):
        doit_mark = stuff.split(" ")[0].ljust(2)
        task = " ".join(stuff.split(" ")[1:]).strip() + "\n"
        output = (
            Fore.GREEN
            + doit_mark
            + f" {path.basename(getcwd())}: "
            + task
            + Style.RESET_ALL
        )
        self.outstream.write(output)


if not in_slurm:
    DOIT_CONFIG = {
        "reporter": GreenReporter,
        # other config here...
        # "cleanforget": True, # Doit will forget about tasks that have been cleaned.
        'backend': 'sqlite3',
        'dep_file': './.doit-db.sqlite'
    }
else:
    DOIT_CONFIG = {
        'backend': 'sqlite3',
        'dep_file': './.doit-db.sqlite'
    }
init(autoreset=True)

# ====================================================================================
# Configuration and Helpers for PyDoit
# ====================================================================================


BASE_DIR = Path(config("BASE_DIR"))
DATA_DIR = Path(config("DATA_DIR"))
RAW_DATA_DIR = Path(config("RAW_DATA_DIR"))
MANUAL_DATA_DIR = Path(config("MANUAL_DATA_DIR"))
OUTPUT_DIR = Path(config("OUTPUT_DIR"))
USER = config("USER") 
OS_TYPE = config("OS_TYPE")

## Helpers for handling Jupyter Notebook tasks
# fmt: off
## Helper functions for automatic execution of Jupyter notebooks
environ["PYDEVD_DISABLE_FILE_VALIDATION"] = "1"
def jupyter_execute_notebook(notebook):
    return f'jupyter nbconvert --execute --to notebook --ClearMetadataPreprocessor.enabled=True --log-level WARN --inplace "{shlex.quote(f"./src/{notebook}.ipynb")}"'
def jupyter_to_html(notebook, output_dir=OUTPUT_DIR):
    return f'jupyter nbconvert --to html --log-level WARN --output-dir="{shlex.quote(str(output_dir))}" "{shlex.quote(f"./src/{notebook}.ipynb")}"'
def jupyter_to_md(notebook, output_dir=OUTPUT_DIR):
    """Requires jupytext"""
    return f'jupytext --to markdown --log-level WARN --output-dir="{shlex.quote(str(output_dir))}" "{shlex.quote(f"./src/{notebook}.ipynb")}"'
def jupyter_to_python(notebook, build_dir):
    """Convert a notebook to a python script"""
    return f'jupyter nbconvert --log-level WARN --to python "{shlex.quote(f"./src/{notebook}.ipynb")}" --output "_{notebook}.py" --output-dir "{shlex.quote(str(build_dir))}"'
def jupyter_clear_output(notebook):
    return f'jupyter nbconvert --log-level WARN --ClearOutputPreprocessor.enabled=True --ClearMetadataPreprocessor.enabled=True --inplace "{shlex.quote(f"./src/{notebook}.ipynb")}"'
    # fmt: on


def copy_file(origin_path, destination_path, mkdir=True):
    """Create a Python action for copying a file."""

    def _copy_file():
        origin = Path(origin_path)
        dest = Path(destination_path)
        if mkdir:
            dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, dest)

    return _copy_file


##################################
## Begin rest of PyDoit tasks here
##################################

def task_config():
    """Create empty directories for data and output if they don't exist"""
    return {
        "actions": ["ipython ./src/settings.py"],
        "targets": [RAW_DATA_DIR, OUTPUT_DIR],
        "file_dep": ["./src/settings.py"],
        "clean": [],
    }



# notebook_tasks = {
#     "get_data.ipynb"
# }
from pathlib import Path

# Automatically discover all notebooks in the ./src directory.
notebook_tasks = {}
for nb_path in Path("./src").glob("*.ipynb"):
    notebook_tasks[nb_path.name] = {
        "file_dep": [nb_path],
        "targets": [Path("./output") / f"_{nb_path.stem}.py"]
    }


def task_convert_notebooks_to_scripts():
    """Convert notebooks to script form to detect changes to source code rather
    than to the notebook's metadata.
    """
    build_dir = Path(OUTPUT_DIR)

    for notebook in notebook_tasks.keys():
        notebook_name = notebook.split(".")[0]
        yield {
            "name": notebook,
            "actions": [
                jupyter_clear_output(notebook_name),
                jupyter_to_python(notebook_name, build_dir),
            ],
            "file_dep": [Path("./src") / notebook],
            "targets": [OUTPUT_DIR / f"_{notebook_name}.py"],
            "clean": True,
            "verbosity": 0,
        }


# fmt: off
def task_run_notebooks():
    """Preps the notebooks for presentation format.
    Execute notebooks if the script version of it has been changed.
    """
    for notebook in notebook_tasks.keys():
        notebook_name = notebook.split(".")[0]
        
        # Check if this is get_data.ipynb - special handling
        is_get_data = notebook_name == "get_data"
        marker_file = OUTPUT_DIR / f"{notebook_name}_processed.marker"
        
        yield {
            "name": notebook,
            "actions": [
                """python -c "import sys; from datetime import datetime; print(f'Start """ + notebook + """: {datetime.now()}', file=sys.stderr)" """,
                # Execute to a new file in output dir, NOT modifying source
                f'jupyter nbconvert --execute --to notebook --output-dir="{OUTPUT_DIR}" --output "{notebook_name}_executed" "{Path("./src")}/{notebook}"',
                # Generate HTML from the executed notebook (not source)
                f'jupyter nbconvert --to html --log-level WARN --output-dir="{OUTPUT_DIR}" "{OUTPUT_DIR}/{notebook_name}_executed.ipynb"',
                # Copy HTML to docs
                copy_file(
                    OUTPUT_DIR / f"{notebook_name}_executed.html",
                    Path("./docs") / f"{notebook_name}.html",
                    mkdir=True,
                ),
                # Create marker file for get_data to avoid re-running
                f'python -c "open(r\'{marker_file}\', \'w\').write(\'processed\')" if not Path(r\'{marker_file}\').exists() else None',
                """python -c "import sys; from datetime import datetime; print(f'End """ + notebook + """: {datetime.now()}', file=sys.stderr)" """,
            ],
            "file_dep": [
                OUTPUT_DIR / f"_{notebook_name}.py",
                *notebook_tasks[notebook]["file_dep"],
            ],
            "targets": [
                OUTPUT_DIR / f"{notebook_name}_executed.ipynb",
                OUTPUT_DIR / f"{notebook_name}_executed.html",
                Path("./docs") / f"{notebook_name}.html",
                marker_file if is_get_data else None,
            ],
            # Skip if get_data has already been processed
            "uptodate": [
                lambda task, values: is_get_data and marker_file.exists()
            ] if is_get_data else None,
            "clean": True,
        }
# fmt: on


# ====================================================================================
# LaTeX compilation
# ====================================================================================

# def task_compile_latex_docs():
#     """Compile the LaTeX documents to PDFs"""
#     file_dep = [
#         "./reports/report_example.tex",
#         "./reports/my_article_header.sty",
#         "./reports/slides_example.tex",
#         "./reports/my_beamer_header.sty",
#         "./reports/my_common_header.sty",
#         "./reports/report_simple_example.tex",
#         "./reports/slides_simple_example.tex",
#         "./src/example_plot.py",
#         "./src/example_table.py",
#     ]
#     targets = [
#         "./reports/report_example.pdf",
#         "./reports/slides_example.pdf",
#         "./reports/report_simple_example.pdf",
#         "./reports/slides_simple_example.pdf",
#     ]

#     return {
#         "actions": [
#             # My custom LaTeX templates
#             "latexmk -xelatex -halt-on-error -cd ./reports/report_example.tex",  # Compile
#             "latexmk -xelatex -halt-on-error -c -cd ./reports/report_example.tex",  # Clean
#             "latexmk -xelatex -halt-on-error -cd ./reports/slides_example.tex",  # Compile
#             "latexmk -xelatex -halt-on-error -c -cd ./reports/slides_example.tex",  # Clean
#             # Simple templates based on small adjustments to Overleaf templates
#             "latexmk -xelatex -halt-on-error -cd ./reports/report_simple_example.tex",  # Compile
#             "latexmk -xelatex -halt-on-error -c -cd ./reports/report_simple_example.tex",  # Clean
#             "latexmk -xelatex -halt-on-error -cd ./reports/slides_simple_example.tex",  # Compile
#             "latexmk -xelatex -halt-on-error -c -cd ./reports/slides_simple_example.tex",  # Clean
#             #
#             # Example of compiling and cleaning in another directory. This often fails, so I don't use it
#             # f"latexmk -xelatex -halt-on-error -cd -output-directory=../_output/ ./reports/report_example.tex",  # Compile
#             # f"latexmk -xelatex -halt-on-error -c -cd -output-directory=../_output/ ./reports/report_example.tex",  # Clean
#         ],
#         "targets": targets,
#         "file_dep": file_dep,
#         "clean": True,
#     }


def copy_docs_src_to_docs():
    """
    Copy all files and subdirectories from the docs_src directory to the _docs directory.
    This function loops through all files in docs_src and copies them individually to _docs,
    preserving the directory structure. It does not delete the contents of _docs beforehand.
    """
    src = Path("docs_src")
    dst = Path("_docs")
    
    # Ensure the destination directory exists
    dst.mkdir(parents=True, exist_ok=True)
    
    # Loop through all files and directories in docs_src
    for item in src.rglob('*'):
        relative_path = item.relative_to(src)
        target = dst / relative_path
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            shutil.copy2(item, target)

def copy_docs_build_to_docs():
    """
    Copy all files and subdirectories from _docs/_build/html to docs.
    This function copies each file individually while preserving the directory structure.
    It does not delete any existing contents in docs.
    After copying, it creates an empty .nojekyll file in the docs directory.
    """
    src = Path("_docs/_build/html")
    dst = Path("docs")
    dst.mkdir(parents=True, exist_ok=True)
    
    # Loop through all files and directories in src
    for item in src.rglob('*'):
        relative_path = item.relative_to(src)
        target = dst / relative_path
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    
    # Touch an empty .nojekyll file in the docs directory.
    (dst / ".nojekyll").touch()