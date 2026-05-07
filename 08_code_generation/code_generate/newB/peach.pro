QT = core xml network concurrent        

CONFIG += c++17 cmdline        
SOURCES += \        
	main.cpp \        
	messageconvert.cpp\
	s0_1_to_w304.cpp\
	codec.cpp\
	s106_to_w204.cpp\
	codec.cpp\
	codec.cpp\
	to_code_Choreography.cpp\

# Default rules for deployment.        
qnx: target.path = / tmp /$${TARGET} / bin        
else: unix:!android : target.path = / opt /$${TARGET} / bin        
!isEmpty(target.path) : INSTALLS += target        
HEADERS += \        
	messageconvert.h\
	s0_1_def.h\
	w304_def.h\
	s0_1_to_w304.h\
	codec.h\
	s106_def.h\
	w204_def.h\
	s106_to_w204.h\
	codec.h\
	codec.h\
	to_code_Choreography.h\