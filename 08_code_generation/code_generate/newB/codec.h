#ifndef CODEC_H
#define CODEC_H

#include "s106_to_w204.h"

#include "s0_1_to_w304.h"

#include <QByteArray>


QString decodeMsg( uchar * pData, int len, W304 &w304);
void encodeMsg(QByteArray& data, W304 &w304);
//校验发送数据，返回值:0 表示收到新数据，1表示收到应答数据,需要将data中的数据发出，-1 表示错误，条过后续操作
int checkObjMaps(QString strVerify,QByteArray& data, W304 &w304);

QString decodeMsg( uchar * pData, int len, S0_1 &s0_1);
void encodeMsg(QByteArray& data, S0_1 &s0_1);
//校验发送数据，返回值:0 表示收到新数据，1表示收到应答数据,需要将data中的数据发出，-1 表示错误，条过后续操作
int checkObjMaps(QString strVerify,QByteArray& data, S0_1 &s0_1);

QString decodeMsg( uchar * pData, int len, W204 &w204);
void encodeMsg(QByteArray& data, W204 &w204);
//校验发送数据，返回值:0 表示收到新数据，1表示收到应答数据,需要将data中的数据发出，-1 表示错误，条过后续操作
int checkObjMaps(QString strVerify,QByteArray& data, W204 &w204);

QString decodeMsg( uchar * pData, int len, S106 &s106);
void encodeMsg(QByteArray& data, S106 &s106);
//校验发送数据，返回值:0 表示收到新数据，1表示收到应答数据,需要将data中的数据发出，-1 表示错误，条过后续操作
int checkObjMaps(QString strVerify,QByteArray& data, S106 &s106);

#endif
