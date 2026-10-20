#######################################################################################################################
# Add the source files to SRCS_LIST
add_source_files(SRCS_LIST
	gqrx/gqrx.h
	gqrx/main.cpp
	gqrx/mainwindow.cpp
	gqrx/mainwindow.h
	gqrx/receiver.cpp
	gqrx/receiver.h
	gqrx/remote_control_settings.cpp
	gqrx/remote_control_settings.h
	gqrx/remote_control.cpp
	gqrx/remote_control.h
	gqrx/recentconfig.cpp
	gqrx/recentconfig.h
	gqrx/file_resources.cpp
)

if(WITH_SHM_IQ_SINK)
	add_source_files(SRCS_LIST
		gqrx/shm_ringbuf.cpp
		gqrx/shm_ringbuf.hpp
		gqrx/shm_ringbuf_impl.cpp
		gqrx/shm_iq_sink.cpp
		gqrx/shm_iq_sink.hpp
		gqrx/shm_iq_sink_impl.cpp
		gqrx/shm_iq_sink_impl.h
	)
endif()

#######################################################################################################################
# Add the UI files to UI_SRCS_LIST
add_source_files(UI_SRCS_LIST
	gqrx/mainwindow.ui
	gqrx/remote_control_settings.ui
)
