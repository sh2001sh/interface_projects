#ifndef CODEC_H
#define CODEC_H

#include "w304_to_iCD304.h"
#include <QByteArray>


QString decodeMsg( uchar * pData, int len, ICD304 &iCD304);
void encodeMsg(QByteArray& data, ICD304 &iCD304);
//校验发送数据，返回值:0 表示收到新数据，1表示收到应答数据,需要将data中的数据发出，-1 表示错误，条过后续操作
int checkObjMaps(QString strVerify,QByteArray& data, ICD304 &iCD304);

QString decodeMsg( uchar * pData, int len, W304 &w304);
void encodeMsg(QByteArray& data, W304 &w304);
//校验发送数据，返回值:0 表示收到新数据，1表示收到应答数据,需要将data中的数据发出，-1 表示错误，条过后续操作
int checkObjMaps(QString strVerify,QByteArray& data, W304 &w304);

#endif
