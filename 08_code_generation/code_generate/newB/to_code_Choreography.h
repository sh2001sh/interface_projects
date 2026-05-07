#ifndef TO_CODE_CHOREOGRAPHY_H
#define TO_CODE_CHOREOGRAPHY_H

#include <QObject>
#include <QMap>
class code_test {
public:
static qulonglong getDstMsg_41(QString name);
static qulonglong getSrcTime_41(QString s1,QString s2);
QMap<QString,uint> getAllSrcTime_41();
QMap<QString,uint> getAllDstTime_41();
int getStatus_41(QString s1);
};

#endif
