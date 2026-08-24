# USER_C_MODULES aggregator (mpftp workspace stub).
# MicroPython's build includes this file when USER_C_MODULES points at the
# workspace (parent of the micropython checkout). Each sibling module that
# provides its own micropython.cmake is pulled in automatically.
set(CMOD_DIR ${CMAKE_CURRENT_LIST_DIR})

# Follow symlinks (-L): workspace modules are often cloned beside this tree
# and linked in. maxdepth 3 matches <workspace>/<module>/micropython.cmake
# (not this aggregator). Exclude only hidden *children* of CMOD_DIR.
execute_process(
    COMMAND find -L ${CMOD_DIR} -mindepth 2 -maxdepth 3 -name micropython.cmake ! -path "${CMOD_DIR}/.*/*" -exec echo -n "{};" \;
    OUTPUT_VARIABLE CMODS
)

foreach(CMOD ${CMODS})
    message("Including file: ${CMOD}")
    include(${CMOD})
endforeach()
