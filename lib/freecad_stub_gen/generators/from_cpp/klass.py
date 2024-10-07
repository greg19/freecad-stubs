import logging
import re
from collections.abc import Iterable
from itertools import chain

from freecad_stub_gen.cpp_code.block import QtSignalBlock, parseClass
from freecad_stub_gen.file_functions import readContent
from freecad_stub_gen.generators.common.annotation_parameter import AnnotationParam
from freecad_stub_gen.generators.common.cpp_function import findFunctionCall
from freecad_stub_gen.generators.common.doc_string import formatDocstring
from freecad_stub_gen.generators.common.names import (
    getClassName,
    getClassWithModulesFromPointer,
    getModuleName,
    useAliasedModule,
)
from freecad_stub_gen.generators.common.return_type_converter.str_wrapper import (
    StrWrapper,
)
from freecad_stub_gen.generators.from_cpp.base import BaseGeneratorFromCpp
from freecad_stub_gen.importable_map import importableMap
from freecad_stub_gen.python_code import indent

logger = logging.getLogger(__name__)


class FreecadStubGeneratorFromCppClass(BaseGeneratorFromCpp):
    """Generate class from cpp code with methods."""

    REG_INIT_TYPE = re.compile(r'::init_type\([^{;]*{')
    REG_CLASS_NAME = re.compile(r'behaviors\(\)\.name\(\s*"([\w.]+)"\s*\);')
    REG_CLASS_DOC = re.compile(r'behaviors\(\).doc\("((?:[^"\\]|\\.|"\s*")+)"\);')

    def _genStub(self, moduleName: str) -> Iterable[str]:
        for match in self.REG_INIT_TYPE.finditer(self.impContent):
            funcCall = findFunctionCall(self.impContent, match.start())
            if (className := self._getClassName(funcCall, moduleName)) is None:
                continue

            self.classNameWithModules = f'{moduleName}.{className}'

            gen = self._findFunctionCallsGen(funcCall)
            result = ''.join(
                chain(
                    self._genQtSignalAndSlots(className),
                    self._genAllMethods(gen, firstParam=AnnotationParam.SELF_PARAM),
                )
            )
            if not result:
                result = 'pass'
            content = indent(result)

            doc = ''
            if importableMap.isImportable(self.classNameWithModules):
                doc = "This class can be imported.\n"
            if docsMatch := self.REG_CLASS_DOC.search(funcCall):
                doc += docsMatch.group(1)
            if doc:
                doc = indent(formatDocstring(doc))

            baseClasses = self._getBaseClasses(className)
            yield f"class {className}{baseClasses}:\n{doc}\n{content}\n"

    def _getClassName(self, funcCall: str, moduleName: str) -> str | None:
        classMatch = self.REG_CLASS_NAME.search(funcCall)
        if not classMatch:
            logger.debug(f'Cannot find function name in {self.baseGenFilePath}')
            return None  # it is probably a template class (ex. SMESH_HypothesisPy<T>)

        className = classMatch.group(1)
        match className.count('.'):
            case 0:
                pass
            case 1 if getModuleName(useAliasedModule(className)) != moduleName:
                msg = (
                    f'Module mismatch: {moduleName} '
                    f'vs {getModuleName(useAliasedModule(className))}'
                )
                raise ValueError(msg)
            case 1:
                className = getClassName(className)
            case _:
                msg = f"Unexpected {className=}"
                raise ValueError(msg)

        return className

    def _genQtSignalAndSlots(self, className: str) -> Iterable[str]:
        if not className.endswith('Py'):
            return

        className = className.removesuffix('Py')
        if not (twinHeaderContent := self._getTwinHeaderContent()):
            return

        found = False
        classObj = parseClass(className, twinHeaderContent)
        for block in classObj.blocks:
            if isinstance(block, QtSignalBlock):
                for item in block:
                    # TODO @PO: [P4] create global context for imports?
                    #  currently it is very annoying to continuously pass
                    #  `requiredImports` - maybe `from contextvars import ContextVar`?
                    yield f'{item.getStrRepr(self.requiredImports)}\n'
                    found = True

        if found:
            yield '\n'

    REG_BASE_CLASS_INHERITANCE = re.compile(
        r"""
(?:public|protected|private)\s+     # access modifier
(?P<baseClass>.+?)\s*               # there may be template class with many parameters
(?:{|                               # either end of expression
,\s*(?:public|protected|private)    # or more base classes
)""",
        re.VERBOSE,
    )

    def _getBaseClasses(self, className: str) -> str:
        if not className.endswith('Py'):
            return ''
        className = className.removesuffix('Py')

        if not (twinHeaderContent := self._getTwinHeaderContent()):
            return ''

        if not (
            match := re.search(
                rf"""
class\s+                # keyword `class`
(?:\w+\s+)?             # there may be optional macro: GuiExport|AppExport
{className}\s*:\s*      # original class name
(?P<inherited>[^{{]*    # all inherited classes until {{
{{)                     # terminating char {{
""",
                twinHeaderContent,
                re.VERBOSE,
            )
        ):
            return ''  # there is no inheritance

        baseClasses = []
        for baseClassMatch in re.finditer(
            self.REG_BASE_CLASS_INHERITANCE, match.group('inherited')
        ):
            baseClass = baseClassMatch.group('baseClass').strip()
            if pythonClass := self._getPythonClass(baseClass):
                baseClasses.append(pythonClass)

        if baseClasses:
            return f"({', '.join(baseClasses)})"

        return ''

    def _getTwinHeaderContent(self) -> str | None:
        currentName = self.baseGenFilePath.stem
        if currentName.endswith('Py'):
            twinName = currentName.removesuffix('Py')
            twinFile = self.baseGenFilePath.with_stem(twinName).with_suffix('.h')
            try:
                return readContent(twinFile)
            except OSError:
                # rare case when twin file is with not standard name
                twinFile = self.baseGenFilePath.with_stem(currentName).with_suffix('.h')
                try:
                    return readContent(twinFile)
                except OSError:
                    logger.exception(f"Cannot read {twinFile}")

        return None

    def _getPythonClass(self, baseClass: str) -> str | None:
        match StrWrapper(baseClass):
            case 'QMainWindow':
                classWithModule = 'qtpy.QtWidgets.QMainWindow'
            case StrWrapper('Q'):
                msg = 'Unknown qt class'
                raise ValueError(msg)
            case StrWrapper(end='Py'):
                classWithModule = getClassWithModulesFromPointer(baseClass)
            case _:
                return None  # Not a python class, or it is a C template class

        if mod := getModuleName(classWithModule):
            self.requiredImports.add(mod)
        return classWithModule
