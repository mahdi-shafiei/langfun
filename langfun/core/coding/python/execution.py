# Copyright 2023 The Langfun Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Python code execution."""

import ast
import contextlib
import multiprocessing
from typing import Any, Callable

from langfun.core.coding.python import errors
from langfun.core.coding.python import parsing
from langfun.core.coding.python import permissions
import pyglove as pg


# Key in the returned dict that represents the final result.
RESULT_KEY = '__result__'
_TLS_CODE_RUN_CONTEXT = '__code_run_context__'


@contextlib.contextmanager
def context(**kwargs):
  """Context manager to inject symbols for code execution."""
  ctx = get_context()
  ctx.update(kwargs)
  pg.object_utils.thread_local_push(_TLS_CODE_RUN_CONTEXT, ctx)

  try:
    yield ctx
  finally:
    pg.object_utils.thread_local_pop(_TLS_CODE_RUN_CONTEXT)


def get_context() -> dict[str, Any]:
  """Gets the current context for code execution."""
  context_stack = pg.object_utils.thread_local_get(_TLS_CODE_RUN_CONTEXT, None)
  return dict(context_stack[-1]) if context_stack else {}


def run(
    code: str, perm: permissions.CodePermission | None = None, **kwargs
) -> dict[str, Any]:
  """Executes Python code.

  Features:
    * Fine-grained execution policy for limiting what APIs could be executed.
      This eliminates the need for sandboxing.
    * It exposes both the final results and intermediate results (variables).

  Args:
    code: Python code to run.
    perm: Permission for the Python code to run.
    **kwargs: The override to the key value pairs provided in `context`,
      which will be exposed as symbols to be referenced by the code.

  Returns:
    A dict of variable names to their evaluated values as the output of the
    code to run. The value for the last line can be accessed by key
    '__result__'.
  """
  # Set up the permission and context.
  perm = perm or permissions.get_permission()
  ctx = dict(get_context())
  ctx.update(kwargs)

  # Parse the code str.
  code, code_block = parsing.PythonCodeParser().parse(code, perm)
  global_vars, local_vars = ctx, {}

  if hasattr(code_block.body[-1], 'value'):
    last_expr = code_block.body.pop()  # pytype: disable=attribute-error
    result_vars = [RESULT_KEY]

    if isinstance(last_expr, ast.Assign):
      for name_node in last_expr.targets:
        result_vars.append(name_node.id)

    last_expr = ast.Expression(last_expr.value)  # pytype: disable=attribute-error

    try:
      # Execute the lines before the last expression.
      exec(compile(code_block, '', mode='exec'), global_vars, local_vars)  # pylint: disable=exec-used

      # Evaluate the last expression.
      result = eval(  # pylint: disable=eval-used
          compile(last_expr, '', mode='eval'), global_vars, local_vars)
    except Exception as e:
      raise errors.CodeError(code, e) from e

    for result_var in result_vars:
      local_vars[result_var] = result
  else:
    try:
      exec(compile(code_block, '', mode='exec'), global_vars, local_vars)  # pylint: disable=exec-used
    except Exception as e:
      raise errors.CodeError(code, e) from e
    local_vars[RESULT_KEY] = list(local_vars.values())[-1]
  return local_vars


def sandbox_call(
    func: Callable[..., Any],
    *args,
    timeout: int | None = None,
    **kwargs) -> Any:
  """Calls a function with sandboxing.

  Args:
    func: Function to call.
    *args: Positional arguments for `func`
    timeout: Execution timeout in seconds. If None, wait `func` to complete.
    **kwargs: Keyword arguments for `func`.

  Returns:
    Return value from `func`.

  Raises:
    TimeoutError: If the execution time exceeds the timeout.
    Exception: Exception raised from `func`.
  """
  def _call(q, *args, **kwargs):
    try:
      q.put(pg.to_json_str(func(*args, **kwargs)))
    except Exception as e:  # pylint: disable=broad-exception-caught
      q.put(e)

  q = multiprocessing.Queue()
  p = multiprocessing.Process(
      target=_call, args=tuple([q] + list(args)), kwargs=kwargs)
  p.start()
  p.join(timeout=timeout)
  if p.is_alive():
    p.terminate()
    raise TimeoutError(f'Execution time exceed {timeout} seconds.')
  x = q.get()
  if isinstance(x, Exception):
    raise x
  return pg.from_json_str(x)


def sandbox_run(
    code: str,
    perm: permissions.CodePermission | None = None,
    timeout: int | None = None,
    **kwargs,
) -> dict[str, Any]:
  """Run Python code with sandboxing.

  Args:
    code: Code to run.
    perm: Permissiong to run.
    timeout: Execution timeout in seconds. If None, wait the code the complete.
    **kwargs: Globals that could be accessed within the code.

  Returns:
    A dict of local variables, with the value of the last expression kept
      in key `__result__`.

  Raises:
    TimeoutError: If the execution time exceeds the timeout.
    Exception: Exception  that are raised from the code.
  """
  return sandbox_call(run, code, perm, time=timeout, **kwargs)