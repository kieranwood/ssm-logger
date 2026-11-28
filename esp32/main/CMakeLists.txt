idf_component_register(SRCS "main.c" "openport.c" "drive_log.c" "console.c" "config.c" "uploader.c" "power.c" INCLUDE_DIRS ".")

# ssm_params.h comes from RomRaider's log_defs.xml (GPL, not in this repo: run ../../fetch_defs.py)
set(LOG_DEFS ${CMAKE_CURRENT_LIST_DIR}/../../definitions/log_defs.xml)
set(GEN_PARAMS ${CMAKE_CURRENT_LIST_DIR}/../gen_params.py)
set(PARAMS_H ${CMAKE_CURRENT_BINARY_DIR}/ssm_params.h)
idf_build_get_property(python PYTHON)
add_custom_command(OUTPUT ${PARAMS_H}
    COMMAND ${python} ${GEN_PARAMS} ${LOG_DEFS} ${PARAMS_H}
    DEPENDS ${LOG_DEFS} ${GEN_PARAMS} ${CMAKE_CURRENT_LIST_DIR}/../../defs.py
    COMMENT "Generating ssm_params.h from log_defs.xml")
add_custom_target(ssm_params DEPENDS ${PARAMS_H})
add_dependencies(${COMPONENT_LIB} ssm_params)
target_include_directories(${COMPONENT_LIB} PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
