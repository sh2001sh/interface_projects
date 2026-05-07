QT = core xml network concurrent        

CONFIG += c++17 cmdline        
SOURCES += \        
	main.cpp \        
	messageconvert.cpp\
	w304_to_iCD304.cpp\
	codec.cpp\

# Default rules for deployment.        
qnx: target.path = / tmp /$${TARGET} / bin        
else: unix:!android : target.path = / opt /$${TARGET} / bin        
!isEmpty(target.path) : INSTALLS += target        
HEADERS += \        
	messageconvert.h\
	iCD304_def.h\
	w304_def.h\
	w304_to_iCD304.h\
	codec.h\