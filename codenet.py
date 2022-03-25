import os
import re
import wget
import json
import tarfile
import functools
import traceback
import subprocess
import concurrent.futures

import numpy as np
import pandas as pd

from tqdm import tqdm
from typing import Union
from difflib import SequenceMatcher
from tree_sitter import Language, Parser, Tree

tqdm.pandas()

P = 8

input_path = "../input/"
root_path = input_path + "Project_CodeNet/"
generated_path = input_path + "generated/"

data_path = root_path + "data/"
generated_data_path = generated_path + "data/"
metadata_path = root_path + "metadata/"
derived_path = root_path + "derived/"
descriptions_path = root_path + "problem_descriptions/"

problem_list_clean_path = generated_path + "problem_list_clean.csv"
generated_pairs_path = generated_path + "generated_pairs.csv"
error_pairs_path = generated_path + "error_pairs.csv"
generated_labels_path = generated_path + "generated_labels.json"

supported_languages = ["Python"]
supported_original_languages = [
    "C++14 (GCC 5.4.1)",
    "C++ (GCC 9.2.1)",
    "C++",
    "JAVA",
    # "Python (3.4.3)",
    # "PyPy3 (7.3.0)",
    "Python (3.8.2)",
    "C++11",
    # "PyPy3 (2.4.0)",
    "C",
    "C (GCC 9.2.1)",
    "C++14 (Clang 3.8.0)",
    "Python",
    "Java (OpenJDK 11.0.6)",
    "C (GCC 5.4.1)",
    # "Python (2.7.6)",
    "C++ (Clang 10.0.0)",
    "Java8 (OpenJDK 1.8.0)",
    "Python3",
    "C++ (GCC 9.2.1 with AC Library v1.1)",
    "C++14",
    "Java (OpenJDK 1.8.0)",
    "C++ (GCC 5.4.1)",
    "C (Clang 3.8.0)",
    "C (Clang 10.0.0)",
    "C++ (Clang 3.8.0)",
    "Java7 (OpenJDK 1.7.0)",
    "C++ (G++ 4.6.4)",
    "C++ (Clang 10.0.0 with AC Library v1.1)",
    # "PyPy2 (5.6.0)",
    "C++11 (GCC 4.8.1)",
    # "PyPy2 (7.3.0)",
    # "Python (3.4.2)",
]

data_url = "https://dax-cdn.cdn.appdomain.cloud/dax-project-codenet/1.0.0"
tar_name = "Project_CodeNet.tar.gz"
tar_path = input_path + tar_name

vendor_python_treesitter_path = "../vendor/tree-sitter-python"
build_languages_path = "../build/my-languages.so"


def parse_treesitter(source_code: str, language: str) -> Tree:

    if language == "Python":
        return PY_PARSER.parse(bytes(source_code, "utf8"))

    assert False, f"Parser for {language} not implemented yet"


def iter_tree(tree: Tree):
    cursor = tree.walk()

    reached_root = False
    while reached_root == False:
        yield cursor.node

        if cursor.goto_first_child():
            continue

        if cursor.goto_next_sibling():
            continue

        retracing = True
        while retracing:
            if not cursor.goto_parent():
                retracing = False
                reached_root = True

            if cursor.goto_next_sibling():
                retracing = False


def generate_tokens_tree(tree: Tree):
    return [node.text.decode() for node in iter_tree(tree) if not node.children and not node.type == "comment"]


def handle_process(
    command: Union[str, list[str]], input: str = None, timeout: float = None
) -> tuple[str, str, int]:
    shell = not isinstance(command, list)

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
        encoding="utf-8",
        errors="ignore",
    )

    try:
        output, error = process.communicate(input, timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        output, error = "", "TLEError: Time limit exceeded"

    return output, error, process.returncode


def extract_error_class_python(error: str, returncode: int) -> str:
    rs = "|".join(
        [
            r"^(\w*Error):.*",
            r"(\w*Warning):.*",
        ]
    )

    p_class = re.compile(rs, re.MULTILINE)
    error_class = p_class.findall(error)
    if not error_class:
        return str(returncode)
    return functools.reduce(lambda acc, x: acc or x, error_class[0], None)


def extract_error_class_extra_python(error: str, returncode: int) -> str:
    rs = "|".join(
        [
            r"^(\w*Error:.*).*",
            r"(\w*Warning:.*).*",
        ]
    )

    p_class_extra = re.compile(rs, re.MULTILINE)
    error_class_extra = p_class_extra.findall(error)
    if not error_class_extra:
        return error
    return functools.reduce(lambda acc, x: acc or x, error_class_extra[0], None)


def extract_error_class_c(error: str, returncode: int) -> str:
    return str(returncode)


def extract_error_class_extra_c(error: str, returncode: int) -> str:
    rs = "|".join(
        [
            r"(undefined reference .*)",
            r"(\*\*\* stack smashing detected \*\*\*: terminated)",
            r"(\*\*\* buffer overflow detected \*\*\*: terminated)",
            r"(munmap_chunk\(\): .*)",
            r"(segmentation fault \(core dumped\))",
            r"(error: .*)",
            r"(relocation truncated to fit: .*)",
            r"(sysmalloc: .*)",
            r"(malloc\(\): .*)",
            r"(free\(\): .*)",
        ]
    )
    p_class_extra = re.compile(rs, re.MULTILINE)
    error_class_extra = p_class_extra.findall(error)
    if not error_class_extra:
        return error
    return functools.reduce(lambda acc, x: acc or x, error_class_extra[0], None)


def extract_error_class_java(error: str, returncode: int) -> str:
    rs = r"Exception in thread \".*?\" ([^:\n]*)"

    p_class = re.compile(rs, re.MULTILINE)
    error_class = p_class.findall(error)
    if not error_class:
        return error
    return error_class[0]


def extract_error_class_extra_java(error: str, returncode: int) -> str:
    rs = r"(Exception .*)"

    p_class_extra = re.compile(rs, re.MULTILINE)
    error_class_extra = p_class_extra.findall(error)
    if not error_class_extra:
        return error
    return error_class_extra[0]


def extract_error_class(row: pd.Series) -> str:
    language, error, returncode = row
    if language == "C":
        return extract_error_class_c(error, returncode)
    if language == "Python":
        return extract_error_class_python(error, returncode)
    if language == "C++":
        return extract_error_class_c(error, returncode)
    if language == "Java":
        return extract_error_class_java(error, returncode)

    return ""


def extract_error_class_extra(row: pd.Series) -> str:
    language, error, returncode = row
    if language == "C":
        return extract_error_class_extra_c(error, returncode)
    if language == "Python":
        return extract_error_class_extra_python(error, returncode)
    if language == "C++":
        return extract_error_class_extra_c(error, returncode)
    if language == "Java":
        return extract_error_class_extra_java(error, returncode)

    return ""


def exec_file_python(file_path: str, input: str = None, timeout: float = 2.0) -> tuple[str, str, int]:
    return handle_process(["python3", file_path], input, timeout)


def exec_file(file_path: str, input: str = None, timeout: float = 2.0, language: str = None) -> tuple[str, str, int]:
    if language == "Python":
        return exec_file_python(file_path, input, timeout)
    raise NotImplementedError


def id2desc(problem_id: str) -> str:
    return descriptions_path + problem_id + ".html"


def id2inout(problem_id: str, name: str = "input") -> str:
    return derived_path + "input_output/data/" + problem_id + "/" + name + ".txt"


def id2submission(
    problem_id: str,
    language: str,
    submission_id: str,
    filename_ext: str,
    data_path: str = data_path,
) -> str:
    return (
        data_path
        + problem_id
        + "/"
        + language
        + "/"
        + submission_id
        + "."
        + filename_ext
    )


def read_submission_file(
    problem_id: str,
    language: str,
    submission_id: str,
    extension: str,
    data_path: str = data_path,
) -> list[str]:
    """
    Read the source code as a list of lines for a given problem and submission id
    the language and extension are also required to complete the path to the file
    """
    with open(
        id2submission(problem_id, language, submission_id, extension, data_path)
    ) as f:
        text = f.readlines()

    return text


def download_codenet(force: bool = False) -> None:
    if os.path.exists(root_path) and not force:
        print("Dataset root dir found. skiping...")
        return

    if not os.path.exists(tar_path) or force:
        wget.download(f"{data_url}/{tar_name}", out=tar_path)

    with tarfile.open(tar_path) as tf:
        tf.extractall(path=data_path)


def clean_codenet(force: bool = False):
    if os.path.exists(problem_list_clean_path) and not force:
        print("Dataset was already cleaned. skiping...")
        return

    file_path = metadata_path + "problem_list.csv"
    print(f"Cleaning {file_path}")

    problem_list_df = pd.read_csv(file_path, index_col="id")

    problem_list_df["time_limit"].fillna(
        problem_list_df["time_limit"].median(), inplace=True
    )
    problem_list_df["memory_limit"].fillna(
        problem_list_df["memory_limit"].median(), inplace=True
    )

    problem_ids = problem_list_df.index.unique()

    input_mask = [
        os.path.exists(id2inout(str(problem_id))) for problem_id in problem_ids
    ]

    problem_list_df = problem_list_df.loc[input_mask]
    problem_ids = problem_list_df.index.unique()

    problem_list_df.to_csv(problem_list_clean_path)


def generate_pairs_task(problem_id: str) -> pd.DataFrame:
    columns = [
        "original_id",
        "changed_id",
        "original_status",
    ]
    dfs = []

    problem_df = pd.read_csv(
        metadata_path + f"{problem_id}.csv", index_col="submission_id"
    )
    if problem_df.empty:
        return pd.DataFrame()

    problem_df = problem_df[
        (problem_df["status"] != "Compile Error")
        & (problem_df["status"] != "Wrong Answer")
        & (problem_df["language"].isin(supported_languages))
        & (problem_df["original_language"].isin(supported_original_languages))
    ]
    grouped_languages = problem_df.groupby("language")

    for language, problem_df in grouped_languages:
        if problem_df.empty:
            continue

        submissions_diff_dfs = []

        user_ids = problem_df["user_id"].unique()
        for user_id in user_ids:
            submission_df = problem_df[problem_df["user_id"] == user_id].sort_values(
                "date"
            )

            if len(submission_df) < 2:
                continue

            submission_ids = submission_df.index.unique()
            for original_id, changed_id in zip(submission_ids, submission_ids[1:]):
                original_status = submission_df.loc[original_id, "status"]
                changed_status = submission_df.loc[changed_id, "status"]
                if not (original_status != "Accepted" and changed_status == "Accepted"):
                    continue

                submissions_diff_dfs.append(
                    (
                        original_id,
                        changed_id,
                        original_status,
                    )
                )

        df = pd.DataFrame(submissions_diff_dfs, columns=columns)
        df["problem_id"] = problem_id
        df["language"] = language
        df["filename_ext"] = problem_df.iloc[0]["filename_ext"]
        dfs.append(df)

    return pd.DataFrame() if not dfs else pd.concat(dfs, ignore_index=True)


def generate_pairs_codenet(force: bool = False):
    if os.path.exists(generated_pairs_path) and not force:
        print("Pairs already generated. skiping...")
        return

    problem_list_df = pd.read_csv(problem_list_clean_path, index_col="id")
    dfs = []

    problem_ids = problem_list_df.index.unique()
    with tqdm(total=len(problem_ids)) as pbar:
        with concurrent.futures.ProcessPoolExecutor(max_workers=P) as executor:
            future_to_problem_id = {
                executor.submit(generate_pairs_task, problem_id): problem_id
                for problem_id in problem_ids
            }

            for future in concurrent.futures.as_completed(future_to_problem_id):
                problem_id = future_to_problem_id[future]

                try:
                    problem_pairs_df = future.result()
                    dfs.append(problem_pairs_df)
                except Exception as exc:
                    print(f"{problem_id} generated an exception: {exc}")
                    traceback.print_exc()
                else:
                    pbar.set_description(f"[Generate Pairs] Processing {problem_id}")
                    pbar.update(1)

    df = pd.concat(dfs, ignore_index=True)
    df.sort_values("original_id").to_csv(generated_pairs_path, index=False)


def generate_error_description_task(
    time_limit: float,
    original_id: str,
    changed_id: str,
    original_status: str,
    problem_id: str,
    language: str,
    filename_ext: str,
) -> dict:
    source_code_path = id2submission(problem_id, language, original_id, filename_ext)

    input_path = id2inout(problem_id, name="input")
    with open(input_path, "r") as f:
        input = f.read()

    timeout = time_limit / 1000 * 1.5

    try:
        output, error, returncode = exec_file(source_code_path, input, timeout, language)
    except (AssertionError, DeprecationWarning) as exc:
        output = ""
        returncode = 1
        error = str(exc)

    error_class = extract_error_class((language, error, returncode))
    error_class_extra = extract_error_class_extra((language, error, returncode))

    return {
        "problem_id": problem_id,
        "original_id": original_id,
        "changed_id": changed_id,
        "language": language,
        "filename_ext": filename_ext,
        "original_status": original_status,
        "returncode": returncode,
        "error_class": error_class,
        "error_class_extra": error_class_extra,
        "error": error,
        "output": output,
    }


def generate_error_description_codenet(force: bool = False) -> pd.DataFrame:
    if os.path.exists(error_pairs_path) and not force:
        print("Error Descriptions already generated. skiping...")
        return

    generated_pairs_df = pd.read_csv(generated_pairs_path)
    problem_list_df = pd.read_csv(problem_list_clean_path, index_col="id")

    time_limit_f = lambda pid: problem_list_df.loc[pid]["time_limit"]

    errs = []
    with tqdm(total=len(generated_pairs_df)) as pbar:
        with concurrent.futures.ProcessPoolExecutor(max_workers=P) as executor:
            future_to_problem_id = {
                executor.submit(
                    generate_error_description_task,
                    time_limit_f(row["problem_id"]),
                    *row,
                ): row
                for _, row in generated_pairs_df.iterrows()
            }

            for future in concurrent.futures.as_completed(future_to_problem_id):
                (
                    original_id,
                    changed_id,
                    original_status,
                    problem_id,
                    language,
                    filename_ext,
                ) = future_to_problem_id[future]
                try:
                    err = future.result()
                    errs.append(err)
                except Exception as exc:
                    print(
                        f"{problem_id}/{language}/({original_id}|{changed_id}).{filename_ext} generated an exception: {exc}"
                    )
                    traceback.print_exc()
                else:
                    pbar.set_description(
                        f"[Generate Error] Processing {problem_id} {original_id}"
                    )
                    pbar.update(1)

    errs_df = pd.DataFrame(errs)
    errs_df.to_csv(error_pairs_path, index=False)


def generate_labels_task(
    problem_id: str,
    original_id: str,
    changed_id: str,
    language: str,
    filename_ext: str,
    original_status: str,
    returncode: int,
    error_class: str,
    error_class_extra: str,
    error: str,
    output: str,
) -> dict:
    original_src = "".join(read_submission_file(problem_id, language, original_id, filename_ext))
    changed_src = "".join(read_submission_file(problem_id, language, changed_id, filename_ext))

    original_tree = parse_treesitter(original_src, language)
    changed_tree = parse_treesitter(changed_src, language)

    original_tokens = generate_tokens_tree(original_tree)
    changed_tokens = generate_tokens_tree(changed_tree)

    s: SequenceMatcher = SequenceMatcher(None, original_tokens, changed_tokens)
    opcodes = [x for x in s.get_opcodes() if x[0] != "equal"]

    original_labels = np.zeros_like(original_tokens, dtype=np.int32)
    changed_labels = np.zeros_like(changed_tokens, dtype=np.int32)
    for _, i1, i2, j1, j2 in opcodes:
        original_labels[i1: max(i1+1, i2)] = 1
        changed_labels[j1: max(j1+1, j2)] = 1
    original_labels = original_labels.tolist()
    changed_labels = changed_labels.tolist()

    return {
        "original_tokens": original_tokens,
        "original_labels": original_labels,
        "changed_tokens": changed_tokens,
        "changed_labels": changed_labels,
        "problem_id": problem_id,
        "original_id": original_id,
        "changed_id": changed_id,
        "language": language,
        "filename_ext": filename_ext,
        "original_status": original_status,
        "returncode": returncode,
        "error_class": error_class,
        "error_class_extra": error_class_extra,
        "error": error,
        "output": output,
    }


def generate_labels_codenet(force: bool = False):
    if os.path.exists(generated_labels_path) and not force:
        print("Labels already generated. skiping...")
        return

    errs_df = pd.read_csv(error_pairs_path, keep_default_na=False)

    labels = []
    with tqdm(total=len(errs_df)) as pbar:
        with concurrent.futures.ProcessPoolExecutor(max_workers=P) as executor:
            future_to_problem_id = {
                executor.submit(
                    generate_labels_task,
                    *row,
                ): row[['original_id', 'changed_id', 'original_status', 'problem_id', 'language', 'filename_ext']]
                for _, row in errs_df.iterrows()
            }

            for future in concurrent.futures.as_completed(future_to_problem_id):
                (
                    original_id,
                    changed_id,
                    original_status,
                    problem_id,
                    language,
                    filename_ext,
                ) = future_to_problem_id[future]
                try:
                    label = future.result()
                    labels.append(label)
                except Exception as exc:
                    print(
                        f"{problem_id}/{language}/({original_id}|{changed_id}).{filename_ext} generated an exception: {exc}"
                    )
                    traceback.print_exc()
                else:
                    pbar.set_description(
                        f"[Generate Labels] Processing {problem_id} {original_id}"
                    )
                    pbar.update(1)

    with open(generated_labels_path, 'w') as f:
        json.dump(labels, f)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(generated_path), exist_ok=True)

    Language.build_library(build_languages_path, [vendor_python_treesitter_path])

    PY_LANGUAGE = Language(build_languages_path, "python")
    PY_PARSER = Parser()
    PY_PARSER.set_language(PY_LANGUAGE)

    download_codenet()
    clean_codenet()
    generate_pairs_codenet()
    generate_error_description_codenet()
    generate_labels_codenet()
