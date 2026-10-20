add_executable(test_squelch_iq_sink
    test_squelch_iq_sink.cpp
    ${CMAKE_SOURCE_DIR}/src/dsp/squelch_iq_sink.cpp
)
target_include_directories(test_squelch_iq_sink PRIVATE ${CMAKE_SOURCE_DIR}/src)
if(Qt6_FOUND)
    target_link_libraries(test_squelch_iq_sink gnuradio::gnuradio-runtime Qt6::Core)
else()
    target_link_libraries(test_squelch_iq_sink gnuradio::gnuradio-runtime Qt5::Core)
endif()
set_property(TARGET test_squelch_iq_sink PROPERTY CXX_STANDARD 14)
add_test(NAME squelch_iq_sink COMMAND test_squelch_iq_sink)
